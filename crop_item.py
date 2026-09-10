"""
crop_item.py — an interactive crop rectangle for a QGraphicsScene.

Supports: dragging the whole box to move it, dragging any of the 4 corner
handles to resize it, and a callback fired (debounced by the caller) when
the geometry settles after a drag.
"""

from PySide6.QtCore import Qt, QRectF, QPointF, Signal, QObject
from PySide6.QtGui import QPen, QBrush, QColor, QCursor
from PySide6.QtWidgets import QGraphicsRectItem, QGraphicsEllipseItem

HANDLE_SIZE = 14
PROOF_RED = QColor("#b23a2e")
FILL = QColor(178, 58, 46, 40)


class _Handle(QGraphicsEllipseItem):
    def __init__(self, corner, parent):
        super().__init__(-HANDLE_SIZE / 2, -HANDLE_SIZE / 2, HANDLE_SIZE, HANDLE_SIZE, parent)
        self.corner = corner  # 'tl','tr','bl','br' (corners) or 't','b','l','r' (edges)
        self.setBrush(QBrush(QColor("#faf9f5")))
        self.setPen(QPen(PROOF_RED, 2))
        self.setFlag(QGraphicsEllipseItem.ItemIsMovable, False)
        self.setFlag(QGraphicsEllipseItem.ItemIgnoresTransformations, True)
        self.setAcceptHoverEvents(True)
        if corner in ("tl", "br"):
            cursor = Qt.SizeFDiagCursor
        elif corner in ("tr", "bl"):
            cursor = Qt.SizeBDiagCursor
        elif corner in ("t", "b"):
            cursor = Qt.SizeVerCursor
        else:  # 'l', 'r'
            cursor = Qt.SizeHorCursor
        self.setCursor(QCursor(cursor))
        self.setZValue(10)

    def mousePressEvent(self, event):
        event.accept()

    def mouseMoveEvent(self, event):
        parent = self.parentItem()
        if parent is not None:
            parent.resize_from_handle(self.corner, event.scenePos())
        event.accept()

    def mouseReleaseEvent(self, event):
        parent = self.parentItem()
        if parent is not None:
            parent.finish_resize()
        event.accept()


class CropRectItem(QGraphicsRectItem):
    """A movable/resizable rectangle. `on_change(rect)` is called (in
    scene coordinates) whenever the box finishes moving or resizing."""

    def __init__(self, rect: QRectF, bounds: QRectF, on_change=None):
        # Normalize immediately: local rect always starts at (0,0), and
        # self.pos() always holds the true scene-space top-left corner.
        # (Mixing a non-zero local rect origin with a separate pos() is
        # what caused the very first corner-drag after construction to
        # jump the whole box — scene_rect() below would silently ignore
        # the local rect's own x()/y() offset.)
        super().__init__(0, 0, rect.width(), rect.height())
        self.setPos(rect.x(), rect.y())
        self.bounds = bounds
        self.on_change = on_change
        self.setPen(QPen(PROOF_RED, 3))
        self.setBrush(QBrush(FILL))
        self.setFlag(QGraphicsRectItem.ItemIsMovable, True)
        self.setFlag(QGraphicsRectItem.ItemSendsScenePositionChanges, True)
        self.setCursor(QCursor(Qt.SizeAllCursor))
        self.setZValue(5)

        self.handles = {c: _Handle(c, self) for c in ("tl", "tr", "bl", "br", "t", "b", "l", "r")}
        self._drag_corner = None
        self._place_handles()

    # ------------------------------------------------------------------
    def _place_handles(self):
        r = self.rect()
        cx, cy = r.center().x(), r.center().y()
        pts = {"tl": r.topLeft(), "tr": r.topRight(),
               "bl": r.bottomLeft(), "br": r.bottomRight(),
               "t": QPointF(cx, r.top()), "b": QPointF(cx, r.bottom()),
               "l": QPointF(r.left(), cy), "r": QPointF(r.right(), cy)}
        for corner, h in self.handles.items():
            h.setPos(pts[corner])

    def set_rect_clamped(self, rect: QRectF):
        # keep the box within the page bounds and non-degenerate
        x = max(self.bounds.left(), min(rect.left(), self.bounds.right() - 4))
        y = max(self.bounds.top(), min(rect.top(), self.bounds.bottom() - 4))
        w = max(4, min(rect.width(), self.bounds.right() - x))
        h = max(4, min(rect.height(), self.bounds.bottom() - y))
        self.setRect(0, 0, w, h)
        self.setPos(x, y)
        self._place_handles()

    def scene_rect(self) -> QRectF:
        r = self.rect()
        p = self.pos()
        return QRectF(p.x() + r.x(), p.y() + r.y(), r.width(), r.height())

    # ------------------------------------------------------------------
    def mousePressEvent(self, event):
        # check if press landed on a handle (handles are children so this
        # only fires for presses on the body itself)
        self._drag_corner = None
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        super().mouseMoveEvent(event)
        # clamp position live while moving the whole box
        p = self.pos()
        r = self.rect()
        x = max(self.bounds.left(), min(p.x(), self.bounds.right() - r.width()))
        y = max(self.bounds.top(), min(p.y(), self.bounds.bottom() - r.height()))
        if (x, y) != (p.x(), p.y()):
            self.setPos(x, y)
        self._place_handles()

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        if self.on_change:
            self.on_change(self.scene_rect())

    # called by the view when a handle child reports a drag
    def resize_from_handle(self, corner, scene_pos: QPointF):
        r = self.scene_rect()
        x0, y0, x1, y1 = r.left(), r.top(), r.right(), r.bottom()
        if "l" in corner:
            x0 = min(scene_pos.x(), x1 - 4)
        if "r" in corner:
            x1 = max(scene_pos.x(), x0 + 4)
        if "t" in corner:
            y0 = min(scene_pos.y(), y1 - 4)
        if "b" in corner:
            y1 = max(scene_pos.y(), y0 + 4)
        x0 = max(self.bounds.left(), x0)
        y0 = max(self.bounds.top(), y0)
        x1 = min(self.bounds.right(), x1)
        y1 = min(self.bounds.bottom(), y1)
        self.set_rect_clamped(QRectF(x0, y0, x1 - x0, y1 - y0))

    def finish_resize(self):
        if self.on_change:
            self.on_change(self.scene_rect())
