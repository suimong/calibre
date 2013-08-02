#!/usr/bin/env python
# vim:fileencoding=utf-8
from __future__ import (unicode_literals, division, absolute_import,
                        print_function)

__license__ = 'GPL v3'
__copyright__ = '2013, Kovid Goyal <kovid at kovidgoyal.net>'

import itertools, operator, os
from types import MethodType
from time import time
from collections import OrderedDict
from threading import Lock, Event, Thread
from Queue import Queue
from functools import wraps, partial

from PyQt4.Qt import (
    QListView, QSize, QStyledItemDelegate, QModelIndex, Qt, QImage, pyqtSignal,
    QPalette, QColor, QItemSelection, QPixmap, QMenu, QApplication, QMimeData,
    QUrl, QDrag, QPoint, QPainter, QRect)

from calibre import fit_image
from calibre.utils.config import prefs

# Drag 'n Drop {{{
def dragMoveEvent(self, event):
    event.acceptProposedAction()

def event_has_mods(self, event=None):
    mods = event.modifiers() if event is not None else \
            QApplication.keyboardModifiers()
    return mods & Qt.ControlModifier or mods & Qt.ShiftModifier

def mousePressEvent(base_class, self, event):
    ep = event.pos()
    if self.indexAt(ep) in self.selectionModel().selectedIndexes() and \
            event.button() == Qt.LeftButton and not self.event_has_mods():
        self.drag_start_pos = ep
    return base_class.mousePressEvent(self, event)

def drag_icon(self, cover, multiple):
    cover = cover.scaledToHeight(120, Qt.SmoothTransformation)
    if multiple:
        base_width = cover.width()
        base_height = cover.height()
        base = QImage(base_width+21, base_height+21,
                QImage.Format_ARGB32_Premultiplied)
        base.fill(QColor(255, 255, 255, 0).rgba())
        p = QPainter(base)
        rect = QRect(20, 0, base_width, base_height)
        p.fillRect(rect, QColor('white'))
        p.drawRect(rect)
        rect.moveLeft(10)
        rect.moveTop(10)
        p.fillRect(rect, QColor('white'))
        p.drawRect(rect)
        rect.moveLeft(0)
        rect.moveTop(20)
        p.fillRect(rect, QColor('white'))
        p.save()
        p.setCompositionMode(p.CompositionMode_SourceAtop)
        p.drawImage(rect.topLeft(), cover)
        p.restore()
        p.drawRect(rect)
        p.end()
        cover = base
    return QPixmap.fromImage(cover)

def drag_data(self):
    m = self.model()
    db = m.db
    rows = self.selectionModel().selectedIndexes()
    selected = list(map(m.id, rows))
    ids = ' '.join(map(str, selected))
    md = QMimeData()
    md.setData('application/calibre+from_library', ids)
    fmt = prefs['output_format']

    def url_for_id(i):
        try:
            ans = db.format_path(i, fmt, index_is_id=True)
        except:
            ans = None
        if ans is None:
            fmts = db.formats(i, index_is_id=True)
            if fmts:
                fmts = fmts.split(',')
            else:
                fmts = []
            for f in fmts:
                try:
                    ans = db.format_path(i, f, index_is_id=True)
                except:
                    ans = None
        if ans is None:
            ans = db.abspath(i, index_is_id=True)
        return QUrl.fromLocalFile(ans)

    md.setUrls([url_for_id(i) for i in selected])
    drag = QDrag(self)
    col = self.selectionModel().currentIndex().column()
    try:
        md.column_name = self.column_map[col]
    except AttributeError:
        md.column_name = 'title'
    drag.setMimeData(md)
    cover = self.drag_icon(m.cover(self.currentIndex().row()),
            len(selected) > 1)
    drag.setHotSpot(QPoint(-15, -15))
    drag.setPixmap(cover)
    return drag

def mouseMoveEvent(base_class, self, event):
    if not self.drag_allowed:
        return
    if self.drag_start_pos is None:
        return base_class.mouseMoveEvent(self, event)

    if self.event_has_mods():
        self.drag_start_pos = None
        return

    if not (event.buttons() & Qt.LeftButton) or \
            (event.pos() - self.drag_start_pos).manhattanLength() \
                    < QApplication.startDragDistance():
        return

    index = self.indexAt(event.pos())
    if not index.isValid():
        return
    drag = self.drag_data()
    drag.exec_(Qt.CopyAction)
    self.drag_start_pos = None

def dragEnterEvent(self, event):
    if int(event.possibleActions() & Qt.CopyAction) + \
        int(event.possibleActions() & Qt.MoveAction) == 0:
        return
    paths = self.paths_from_event(event)

    if paths:
        event.acceptProposedAction()

def dropEvent(self, event):
    paths = self.paths_from_event(event)
    event.setDropAction(Qt.CopyAction)
    event.accept()
    self.files_dropped.emit(paths)

def paths_from_event(self, event):
    '''
    Accept a drop event and return a list of paths that can be read from
    and represent files with extensions.
    '''
    md = event.mimeData()
    if md.hasFormat('text/uri-list') and not \
            md.hasFormat('application/calibre+from_library'):
        urls = [unicode(u.toLocalFile()) for u in md.urls()]
        return [u for u in urls if os.path.splitext(u)[1] and
                os.path.exists(u)]

def setup_dnd_interface(cls_or_self):
    if isinstance(cls_or_self, type):
        cls = cls_or_self
        base_class = cls.__bases__[0]
        fmap = globals()
        for x in (
            'dragMoveEvent', 'event_has_mods', 'mousePressEvent', 'mouseMoveEvent',
            'drag_data', 'drag_icon', 'dragEnterEvent', 'dropEvent', 'paths_from_event'):
            func = fmap[x]
            if x in {'mouseMoveEvent', 'mousePressEvent'}:
                func = partial(func, base_class)
            setattr(cls, x, MethodType(func, None, cls))
    else:
        self = cls_or_self
        self.drag_allowed = True
        self.drag_start_pos = None
        self.setDragEnabled(True)
        self.setDragDropOverwriteMode(False)
        self.setDragDropMode(self.DragDrop)
# }}}

# Manage slave views {{{
def sync(func):
    @wraps(func)
    def ans(self, *args, **kwargs):
        if self.break_link or self.current_view is self.main_view:
            return
        with self:
            return func(self, *args, **kwargs)
    return ans

class AlternateViews(object):

    def __init__(self, main_view):
        self.views = {None:main_view}
        self.stack_positions = {None:0}
        self.current_view = self.main_view = main_view
        self.stack = None
        self.break_link = False
        self.main_connected = False

    def set_stack(self, stack):
        self.stack = stack
        self.stack.addWidget(self.main_view)

    def add_view(self, key, view):
        self.views[key] = view
        self.stack_positions[key] = self.stack.count()
        self.stack.addWidget(view)
        self.stack.setCurrentIndex(0)
        view.setModel(self.main_view._model)
        view.selectionModel().currentChanged.connect(self.slave_current_changed)
        view.selectionModel().selectionChanged.connect(self.slave_selection_changed)
        view.sort_requested.connect(self.main_view.sort_by_named_field)
        view.files_dropped.connect(self.main_view.files_dropped)

    def show_view(self, key=None):
        view = self.views[key]
        if view is self.current_view:
            return
        self.stack.setCurrentIndex(self.stack_positions[key])
        self.current_view = view
        if view is not self.main_view:
            self.main_current_changed(self.main_view.currentIndex())
            self.main_selection_changed()
            view.shown()
            if not self.main_connected:
                self.main_connected = True
                self.main_view.selectionModel().currentChanged.connect(self.main_current_changed)
                self.main_view.selectionModel().selectionChanged.connect(self.main_selection_changed)
            view.setFocus(Qt.OtherFocusReason)

    def set_database(self, db, stage=0):
        for view in self.views.itervalues():
            if view is not self.main_view:
                view.set_database(db, stage=stage)

    def __enter__(self):
        self.break_link = True

    def __exit__(self, *args):
        self.break_link = False

    @sync
    def slave_current_changed(self, current, *args):
        self.main_view.set_current_row(current.row(), for_sync=True)

    @sync
    def slave_selection_changed(self, *args):
        rows = {r.row() for r in self.current_view.selectionModel().selectedIndexes()}
        self.main_view.select_rows(rows, using_ids=False, change_current=False, scroll=False)

    @sync
    def main_current_changed(self, current, *args):
        self.current_view.set_current_row(current.row())

    @sync
    def main_selection_changed(self, *args):
        rows = {r.row() for r in self.main_view.selectionModel().selectedIndexes()}
        self.current_view.select_rows(rows)

    def set_context_menu(self, menu):
        for view in self.views.itervalues():
            if view is not self.main_view:
                view.set_context_menu(menu)
# }}}

# Caching and rendering of covers {{{
class CoverCache(dict):

    def __init__(self, limit=200):
        self.items = OrderedDict()
        self.lock = Lock()
        self.limit = limit

    def invalidate(self, book_id):
        with self.lock:
            self.items.pop(book_id, None)

    def __getitem__(self, key):
        ' Must be called in the GUI thread '
        with self.lock:
            ans = self.items.pop(key, False)  # pop() so that item is moved to the top
            if ans is not False:
                if isinstance(ans, QImage):
                    # Convert to QPixmap, since rendering QPixmap is much
                    # faster
                    ans = QPixmap.fromImage(ans)
                self.items[key] = ans

        return ans

    def set(self, key, val):
        with self.lock:
            self.items.pop(key, None)  # pop() so that item is moved to the top
            self.items[key] = val
            if len(self.items) > self.limit:
                del self.items[next(self.items.iterkeys())]

    def clear(self):
        with self.lock:
            self.items.clear()

    def __hash__(self):
        return id(self)

class CoverDelegate(QStyledItemDelegate):

    def __init__(self, parent, width, height):
        super(CoverDelegate, self).__init__(parent)
        self.cover_size = QSize(width, height)
        self.item_size = self.cover_size + QSize(8, 8)
        self.spacing = max(10, min(50, int(0.1 * width)))
        self.cover_cache = CoverCache()
        self.render_queue = Queue()

    def sizeHint(self, option, index):
        return self.item_size

    def paint(self, painter, option, index):
        QStyledItemDelegate.paint(self, painter, option, QModelIndex())  # draw the hover and selection highlights
        db = index.model().db
        try:
            book_id = db.id(index.row())
        except (ValueError, IndexError, KeyError):
            return
        db = db.new_api
        cdata = self.cover_cache[book_id]
        painter.save()
        try:
            rect = option.rect
            rect.adjust(4, 4, -4, -4)
            if cdata is None or cdata is False:
                title = db.field_for('title', book_id, default_value='')
                painter.drawText(rect, Qt.AlignCenter|Qt.TextWordWrap, title)
                if cdata is False:
                    self.render_queue.put(book_id)
            else:
                dx = max(0, int((rect.width() - cdata.width())/2.0))
                dy = max(0, rect.height() - cdata.height())
                rect.adjust(dx, dy, -dx, 0)
                painter.drawPixmap(rect, cdata)
        finally:
            painter.restore()

def join_with_timeout(q, timeout=2):
    q.all_tasks_done.acquire()
    try:
        endtime = time() + timeout
        while q.unfinished_tasks:
            remaining = endtime - time()
            if remaining <= 0.0:
                raise RuntimeError('Waiting for queue to clear timed out')
            q.all_tasks_done.wait(remaining)
    finally:
        q.all_tasks_done.release()
# }}}

class GridView(QListView):

    update_item = pyqtSignal(object)
    sort_requested = pyqtSignal(object, object)
    files_dropped = pyqtSignal(object)

    def __init__(self, parent):
        QListView.__init__(self, parent)
        setup_dnd_interface(self)
        pal = QPalette(self.palette())
        r = g = b = 0x50
        pal.setColor(pal.Base, QColor(r, g, b))
        pal.setColor(pal.Text, QColor(Qt.white if (r + g + b)/3.0 < 128 else Qt.black))
        self.setPalette(pal)
        self.setUniformItemSizes(True)
        self.setWrapping(True)
        self.setFlow(self.LeftToRight)
        # We cannot set layout mode to batched, because that breaks
        # restore_vpos()
        # self.setLayoutMode(self.Batched)
        self.setResizeMode(self.Adjust)
        self.setSelectionMode(self.ExtendedSelection)
        self.setVerticalScrollMode(self.ScrollPerPixel)
        self.delegate = CoverDelegate(self, 135, 180)
        self.setItemDelegate(self.delegate)
        self.setSpacing(self.delegate.spacing)
        self.ignore_render_requests = Event()
        self.render_thread = None
        self.update_item.connect(self.re_render, type=Qt.QueuedConnection)
        self.doubleClicked.connect(parent.iactions['View'].view_triggered)
        self.gui = parent
        self.context_menu = None

    def shown(self):
        if self.render_thread is None:
            self.render_thread = Thread(target=self.render_covers)
            self.render_thread.daemon = True
            self.render_thread.start()

    def render_covers(self):
        q = self.delegate.render_queue
        while True:
            book_id = q.get()
            try:
                if book_id is None:
                    return
                if self.ignore_render_requests.is_set():
                    continue
                try:
                    self.render_cover(book_id)
                except:
                    import traceback
                    traceback.print_exc()
            finally:
                q.task_done()

    def render_cover(self, book_id):
        cdata = self.model().db.new_api.cover(book_id)
        if self.ignore_render_requests.is_set():
            return
        if cdata is not None:
            p = QImage()
            p.loadFromData(cdata)
            cdata = None
            if not p.isNull():
                width, height = p.width(), p.height()
                scaled, nwidth, nheight = fit_image(width, height, self.delegate.cover_size.width(), self.delegate.cover_size.height())
                if scaled:
                    if self.ignore_render_requests.is_set():
                        return
                    p = p.scaled(nwidth, nheight, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
                cdata = p
        self.delegate.cover_cache.set(book_id, cdata)
        self.update_item.emit(book_id)

    def re_render(self, book_id):
        m = self.model()
        try:
            index = m.db.row(book_id)
        except (IndexError, ValueError, KeyError):
            return
        self.update(m.index(index, 0))

    def shutdown(self):
        self.ignore_render_requests.set()
        self.delegate.render_queue.put(None)

    def set_database(self, newdb, stage=0):
        if stage == 0:
            self.ignore_render_requests.set()
            try:
                self.model().db.new_api.remove_cover_cache(self.delegate.cover_cache)
            except AttributeError:
                pass  # db is None
            newdb.new_api.add_cover_cache(self.delegate.cover_cache)
            try:
                # Use a timeout so that if, for some reason, the render thread
                # gets stuck, we dont deadlock, future covers wont get
                # rendered, but this is better than a deadlock
                join_with_timeout(self.delegate.render_queue)
            except RuntimeError:
                print ('Cover rendering thread is stuck!')
            finally:
                self.ignore_render_requests.clear()
        else:
            self.delegate.cover_cache.clear()

    def select_rows(self, rows):
        sel = QItemSelection()
        sm = self.selectionModel()
        m = self.model()
        # Create a range based selector for each set of contiguous rows
        # as supplying selectors for each individual row causes very poor
        # performance if a large number of rows has to be selected.
        for k, g in itertools.groupby(enumerate(rows), lambda (i,x):i-x):
            group = list(map(operator.itemgetter(1), g))
            sel.merge(QItemSelection(m.index(min(group), 0), m.index(max(group), 0)), sm.Select)
        sm.select(sel, sm.ClearAndSelect)

    def set_current_row(self, row):
        sm = self.selectionModel()
        sm.setCurrentIndex(self.model().index(row, 0), sm.NoUpdate)

    def set_context_menu(self, menu):
        self.context_menu = menu

    def contextMenuEvent(self, event):
        if self.context_menu is not None:
            lv = self.gui.library_view
            menu = self._temp_menu = QMenu(self)
            sm = QMenu(_('Sort by'), menu)
            db = self.model().db
            for col in lv.visible_columns:
                m = db.metadata_for_field(col)
                last = self.model().sorted_on
                ascending = True
                extra = ''
                if last[0] == col:
                    ascending = not last[1]
                    extra = ' [%s]' % _('reverse')
                sm.addAction('%s%s' % (m.get('name', col), extra), partial(self.do_sort, col, ascending))

            for ac in self.context_menu.actions():
                menu.addAction(ac)
            menu.addMenu(sm)
            menu.popup(event.globalPos())
            event.accept()

    def do_sort(self, column, ascending):
        self.sort_requested.emit(column, ascending)

    def restore_vpos(self, vpos):
        self.verticalScrollBar().setValue(vpos)

    def restore_hpos(self, hpos):
        pass

setup_dnd_interface(GridView)