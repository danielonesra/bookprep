import time
import numpy as np
from PySide6.QtCore import Qt, QRectF, QPointF, Signal
from PySide6.QtGui import QPainter, QBrush, QColor, QPen, QImage, QPixmap
from PySide6.QtWidgets import QGraphicsView, QGraphicsScene


class ZoomPanGraphicsView(QGraphicsView):
    """A QGraphicsView with mouse-wheel zoom (anchored under the cursor),
    click-and-drag panning, and programmatic zoom in/out/fit/actual-size.

    This is scene-agnostic: it doesn't own or manage the scene's content
    (items can still handle their own mouse events, e.g. a movable crop
    rectangle — panning only kicks in when a click lands on empty space).
    Call `set_fit_rect()` whenever the content bounds change (e.g. a new
    page loaded) so "Fit" and auto-refit-on-resize know what to fit to.

    Emits zoom_changed(float) with the current zoom factor (1.0 = 100%).
    """

    zoom_changed = Signal(float)

    MIN_ZOOM = 0.05
    MAX_ZOOM = 12.0

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setRenderHints(QPainter.SmoothPixmapTransform | QPainter.Antialiasing)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorViewCenter)
        self.setDragMode(QGraphicsView.ScrollHandDrag)

        self._zoom = 1.0
        self._fit_mode = True  # auto-fit to the viewport until the user zooms manually
        self._fit_rect = None

    # ------------------------------------------------------------------
    def set_fit_rect(self, rect: QRectF, refit_if_fitting=True):
        """Tell the view what rectangle 'Fit' / auto-refit should target
        (typically the scene's content bounds). If the view is currently
        in fit mode (or has no content yet), it re-fits immediately."""
        was_empty = self._fit_rect is None
        self._fit_rect = rect
        if refit_if_fitting and (self._fit_mode or was_empty):
            self.fit_to_window()

    def clear_fit_rect(self):
        self._fit_rect = None

    # ---- zoom controls --------------------------------------------------
    def fit_to_window(self):
        if self._fit_rect is None or self._fit_rect.isEmpty():
            return
        self.fitInView(self._fit_rect, Qt.KeepAspectRatio)
        self._zoom = self.transform().m11()
        self._fit_mode = True
        self.zoom_changed.emit(self._zoom)

    def zoom_actual_size(self):
        if self._fit_rect is None:
            return
        self.resetTransform()
        self._zoom = 1.0
        self._fit_mode = False
        self.zoom_changed.emit(self._zoom)

    def zoom_by(self, factor):
        if self._fit_rect is None:
            return
        new_zoom = max(self.MIN_ZOOM, min(self.MAX_ZOOM, self._zoom * factor))
        applied = new_zoom / self._zoom
        if applied == 1.0:
            return
        self.scale(applied, applied)
        self._zoom = new_zoom
        self._fit_mode = False
        self.zoom_changed.emit(self._zoom)

    def zoom_in(self):
        self.zoom_by(1.25)

    def zoom_out(self):
        self.zoom_by(1 / 1.25)

    # ------------------------------------------------------------------
    def wheelEvent(self, event):
        if self._fit_rect is not None and event.modifiers() & Qt.ControlModifier:
            factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
            self.zoom_by(factor)
            event.accept()
        else:
            super().wheelEvent(event)  # normal scroll

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._fit_mode and self._fit_rect is not None:
            self.fit_to_window()


class ZoomableImageView(ZoomPanGraphicsView):
    """A self-contained zoomable/pannable single-image viewer (owns its
    own scene) — used for the proof preview, where the whole point is to
    inspect a fully rendered page in detail rather than a fixed thumbnail.

    Also supports a manual paint "touch-up" layer: a transparent overlay
    sitting on top of the rendered page that the user can paint pure
    black/white onto directly, at whatever brush size (down to 1 image
    pixel). The overlay is a separate QGraphicsPixmapItem from the base
    render, specifically so that set_pixmap() (called every time the
    algorithmic pipeline produces a fresh render — e.g. after a settings
    tweak) never wipes out in-progress touch-ups: only the base layer
    gets swapped, the overlay is untouched unless its size no longer
    matches (see set_pixmap()).

    Painting always happens in image-pixel coordinates (via
    mapToScene()), not view/screen coordinates, so brush size and stroke
    precision are unaffected by the current zoom level.
    """

    retouch_changed = Signal()  # emitted once per completed stroke (on mouse release)
    selection_changed = Signal(bool)  # emitted when a select-mode rectangle becomes valid/cleared
    tool_mode_changed = Signal(str)  # "pan" | "paint" | "select" | "paste" -- effective mode,
    # including "paste" (an orthogonal state, not one set_tool_mode() takes directly) so UI
    # buttons can stay in sync even when paste mode exits itself (Escape/right-click)
    undo_available_changed = Signal(bool)  # emitted whenever undo() becomes possible/impossible

    # Cap on how many retouch-layer snapshots undo() can hold. Each snapshot is a
    # full copy of the page-sized retouch QImage (e.g. ~78MB uncompressed for a
    # 3600x5400 ARGB32 page), so this is a real memory/undo-depth tradeoff, not
    # just an arbitrary number -- raise with that in mind.
    _UNDO_STACK_LIMIT = 15

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.pixmap_item = None

        self.setBackgroundBrush(QBrush(QColor("#e7e5dd")))
        self.setFrameShape(QGraphicsView.NoFrame)

        self._retouch_item = None
        self._retouch_image = None  # QImage, ARGB32, alpha=0 where unpainted
        self._tool_mode = "pan"     # "pan" | "paint" | "select"
        self._brush_size = 8
        self._paint_color = 0  # 0 = black, 255 = white
        self._painting = False
        self._last_paint_pos = None

        self._selecting = False
        self._select_start = None       # QPointF, scene coords
        self._selection_rect = None     # QRectF, scene coords -- finalized selection
        self._selection_item = None     # QGraphicsRectItem, dashed outline
        self._active_painter = None     # QPainter kept open for a whole stroke -- see
        # _begin_stroke()/_end_stroke(): opening/closing a QPainter on a full page-sized
        # QImage is surprisingly expensive (~19ms measured on a 3600x5400 image), so doing
        # it once per stroke instead of once per mouse-move event is roughly a 20x speedup
        self._last_refresh_time = 0.0

        self._clipboard_image = None    # QImage -- the copied patch
        self._paste_armed = False
        self._paste_preview_item = None
        self._paste_return_mode = "pan"  # tool mode to restore after a paste is done

        self._undo_stack = []  # list of QImage snapshots of _retouch_image, oldest first

    def set_pixmap(self, pixmap):
        had_item = self.pixmap_item is not None
        if self.pixmap_item is not None:
            self._scene.removeItem(self.pixmap_item)
        self.pixmap_item = self._scene.addPixmap(pixmap)
        self.pixmap_item.setZValue(0)

        size = pixmap.size()
        if self._retouch_image is not None and self._retouch_image.size() != size:
            # Base render changed size (trim/DPI setting changed) --
            # the old overlay's pixel positions no longer mean anything.
            # Drop the in-memory layer; whatever was saved to disk is
            # untouched and will simply stop applying (render_final
            # checks shape before applying a saved mask too).
            self._end_stroke()  # don't leave a painter dangling on the image we're about to drop
            self._retouch_image = None
            if self._retouch_item is not None:
                self._scene.removeItem(self._retouch_item)
                self._retouch_item = None
            self._clear_selection()  # old selection's coordinates are meaningless too
            self._clear_undo_stack()  # snapshots were sized for the old image; meaningless now
        if self._retouch_image is None:
            self._retouch_image = QImage(size, QImage.Format_ARGB32_Premultiplied)
            self._retouch_image.fill(Qt.transparent)
        if self._retouch_item is None:
            self._retouch_item = self._scene.addPixmap(QPixmap.fromImage(self._retouch_image.copy()))
        self._retouch_item.setZValue(1)

        rect = QRectF(pixmap.rect())
        self._scene.setSceneRect(rect)
        self.set_fit_rect(rect, refit_if_fitting=(self._fit_mode or not had_item))

    def clear(self):
        self._scene.clear()
        self.pixmap_item = None
        self._retouch_item = None
        self._retouch_image = None
        self._selection_item = None
        self._selection_rect = None
        self._paste_preview_item = None
        self._paste_armed = False
        self._clear_undo_stack()
        self.clear_fit_rect()

    # ---- touch-up / paint mode -----------------------------------------
    def set_paint_mode(self, enabled: bool):
        """Kept for backwards compatibility with existing call sites;
        equivalent to set_tool_mode("paint" if enabled else "pan")."""
        self.set_tool_mode("paint" if enabled else "pan")

    @property
    def tool_mode(self):
        return self._tool_mode

    @property
    def is_paste_armed(self):
        return self._paste_armed

    def set_tool_mode(self, mode: str):
        """mode: "pan" (default click-drag panning), "paint" (freehand
        brush), or "select" (drag a rectangle for copy/paste)."""
        assert mode in ("pan", "paint", "select")
        if self._selecting:
            self._selecting = False  # abandon an in-progress drag
        self._end_stroke()  # safety net: switching mode mid-stroke shouldn't leak a painter
        self._disarm_paste()
        self._tool_mode = mode
        self.setDragMode(QGraphicsView.ScrollHandDrag if mode == "pan" else QGraphicsView.NoDrag)
        cursor = {"pan": Qt.ArrowCursor, "paint": Qt.CrossCursor, "select": Qt.CrossCursor}[mode]
        self.viewport().setCursor(cursor)
        self._report_mode()

    def _report_mode(self):
        self.tool_mode_changed.emit("paste" if self._paste_armed else self._tool_mode)

    def set_brush_size(self, px: int):
        self._brush_size = max(1, int(px))

    def set_paint_color(self, value: int):
        """value: 0 for black, 255 for white."""
        self._paint_color = 0 if value == 0 else 255

    def get_retouch_mask(self):
        """Returns the current overlay as a single-channel uint8 numpy
        array (proc.RETOUCH_NONE=128 where unpainted, 0/255 where
        painted), suitable for Project.save_retouch_mask(). None if
        there's no image loaded yet."""
        if self._retouch_image is None:
            return None
        img = self._retouch_image.convertToFormat(QImage.Format_ARGB32)
        w, h = img.width(), img.height()
        ptr = img.bits()
        arr = np.frombuffer(ptr, dtype=np.uint8, count=h * img.bytesPerLine()).reshape(h, img.bytesPerLine())
        arr = arr[:, :w * 4].reshape(h, w, 4)  # BGRA on little-endian platforms
        alpha = arr[:, :, 3]
        blue = arr[:, :, 0]  # B==G==R for the pure black/white we paint
        mask = np.full((h, w), 128, dtype=np.uint8)
        painted = alpha >= 128
        mask[painted] = np.where(blue[painted] >= 128, 255, 0)
        return mask

    def _build_retouch_qimage(self, mask):
        """Shared by load_retouch_mask() and set_retouch_pixels(): turn a
        proc.RETOUCH_NONE/0/255-encoded numpy mask into the ARGB32
        QImage the overlay item displays. Returns None if there's
        nowhere to put it yet (no pixmap loaded) or the mask doesn't
        match the current canvas size."""
        size = self.pixmap_item.pixmap().size() if self.pixmap_item else None
        if size is None:
            return None
        img = QImage(size, QImage.Format_ARGB32_Premultiplied)
        img.fill(Qt.transparent)
        if mask is not None and mask.shape == (size.height(), size.width()):
            rgba = np.zeros((mask.shape[0], mask.shape[1], 4), dtype=np.uint8)
            painted = mask != 128
            rgba[painted, 0] = mask[painted]
            rgba[painted, 1] = mask[painted]
            rgba[painted, 2] = mask[painted]
            rgba[painted, 3] = 255
            qimg = QImage(rgba.data, mask.shape[1], mask.shape[0], mask.shape[1] * 4,
                           QImage.Format_ARGB32).copy()
            img = qimg.convertToFormat(QImage.Format_ARGB32_Premultiplied)
        return img

    def load_retouch_mask(self, mask):
        """Populate the overlay from a saved mask (proc.RETOUCH_NONE/0/255
        encoding), or clear it if mask is None. Must be called after
        set_pixmap() so the overlay item/size already exist. Use this
        for an actual page switch; for repositioning the SAME page's
        in-progress layer (e.g. after a margin edit shifts the content
        under it), use set_retouch_pixels() instead so undo history
        isn't wiped out."""
        if self._retouch_item is None:
            return
        img = self._build_retouch_qimage(mask)
        if img is None:
            return
        self._retouch_image = img
        self._retouch_item.setPixmap(QPixmap.fromImage(self._retouch_image.copy()))
        # A different page's paint history shouldn't be undoable from here --
        # each page's retouch layer gets its own undo history from scratch.
        self._clear_undo_stack()

    def set_retouch_pixels(self, mask):
        """Like load_retouch_mask(), but leaves the undo stack alone --
        for repositioning the current page's overlay in place (its
        content moved under it, e.g. a margin/size-override edit) rather
        than replacing it with a different page's saved layer."""
        if self._retouch_item is None:
            return
        img = self._build_retouch_qimage(mask)
        if img is None:
            return
        self._retouch_image = img
        self._retouch_item.setPixmap(QPixmap.fromImage(self._retouch_image.copy()))

    def clear_retouch(self):
        if self._retouch_image is None:
            return
        self._push_undo_snapshot()
        self._retouch_image.fill(Qt.transparent)
        self._retouch_item.setPixmap(QPixmap.fromImage(self._retouch_image.copy()))
        self.retouch_changed.emit()

    # ---- undo -------------------------------------------------------------
    def _push_undo_snapshot(self):
        """Record the current retouch layer so undo() can restore it.
        Call this right before making a change that should be undoable
        (start of a paint stroke, a paste stamp, or a full clear) -- i.e.
        it snapshots the *pre*-change state."""
        if self._retouch_image is None:
            return
        was_empty = not self._undo_stack
        self._undo_stack.append(self._retouch_image.copy())
        if len(self._undo_stack) > self._UNDO_STACK_LIMIT:
            self._undo_stack.pop(0)
        if was_empty:
            self.undo_available_changed.emit(True)

    def _clear_undo_stack(self):
        had_undo = bool(self._undo_stack)
        self._undo_stack = []
        if had_undo:
            self.undo_available_changed.emit(False)

    def can_undo(self):
        return bool(self._undo_stack)

    def undo(self):
        """Restore the retouch layer to its state before the last paint
        stroke, paste stamp, or clear. Returns True if something was
        actually undone."""
        if not self._undo_stack or self._retouch_image is None:
            return False
        self._end_stroke()  # don't leave a painter open on the image we're about to replace
        self._retouch_image = self._undo_stack.pop()
        self._retouch_item.setPixmap(QPixmap.fromImage(self._retouch_image.copy()))
        self.retouch_changed.emit()
        if not self._undo_stack:
            self.undo_available_changed.emit(False)
        return True

    def _make_pen(self):
        color = QColor(0, 0, 0, 255) if self._paint_color == 0 else QColor(255, 255, 255, 255)
        pen = QPen(color, self._brush_size)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        return pen

    _DISPLAY_REFRESH_INTERVAL = 0.04  # ~25fps -- see _sync_display()

    def _open_painter(self):
        p = QPainter(self._retouch_image)
        p.setRenderHint(QPainter.Antialiasing, False)
        p.setCompositionMode(QPainter.CompositionMode_Source)
        p.setPen(self._make_pen())
        return p

    def _begin_stroke(self):
        """Open one QPainter session for an entire stroke (mouse-press to
        mouse-release), rather than one per mouse-move event -- opening/
        closing a QPainter on a full page-sized QImage is expensive
        (~19ms measured on a 3600x5400 image) essentially regardless of
        what gets drawn, so doing it once per stroke instead of once per
        mouse-move is roughly a 20x speedup and the difference between
        painting feeling smooth vs. laggy on a real page."""
        if self._retouch_image is None:
            return
        if self._active_painter is not None:
            self._end_stroke()  # safety net: shouldn't normally still be open
        self._push_undo_snapshot()
        self._active_painter = self._open_painter()
        self._last_refresh_time = 0.0  # force an immediate display refresh on the first _paint_at

    def _end_stroke(self):
        if self._active_painter is not None:
            self._active_painter.end()
            self._active_painter = None
        self._sync_display(force=True)  # guarantee the final state is always shown

    def _sync_display(self, force=False):
        """Push self._retouch_image to the displayed pixmap item.
        Throttled during an open stroke (~25fps) rather than done on
        every single mouse-move, and briefly closes/reopens the active
        painter around the conversion -- QPixmap.fromImage() on a QImage
        that still has a QPainter actively open on it is ~60x more
        expensive than on the same image right after .end() (measured:
        ~69ms vs ~1ms on a 3600x5400 image), apparently forced into a
        deep copy rather than a cheap shared reference. Closing just for
        the moment of conversion, then reopening to keep drawing, gets
        the fast path without losing the open session's state (pen etc.
        gets reapplied on reopen)."""
        if not force:
            now = time.monotonic()
            if now - self._last_refresh_time < self._DISPLAY_REFRESH_INTERVAL:
                return
        reopen = self._active_painter is not None
        if reopen:
            self._active_painter.end()
        self._retouch_item.setPixmap(QPixmap.fromImage(self._retouch_image.copy()))
        if reopen:
            self._active_painter = self._open_painter()
        # Stamp the time *after* the (expensive) sync work is done, not before --
        # stamping before it means the sync's own ~40-50ms cost gets folded into
        # the "time since last refresh" measured on the very next call, which
        # makes it look like the interval has already elapsed and triggers a
        # sync on every single call (a feedback loop that defeats the throttle
        # entirely -- confirmed by profiling: 300 _paint_at calls produced 301
        # syncs, not the handful the throttle was meant to allow).
        self._last_refresh_time = time.monotonic()

    def _paint_at(self, scene_pos):
        if self._retouch_image is None:
            return
        # Reuse the stroke's open painter if there is one (the fast,
        # normal interactive path); otherwise open/draw/close a one-shot
        # painter, so direct calls (tests, or any other caller that
        # doesn't bother with _begin_stroke/_end_stroke) still work.
        owns_painter = self._active_painter is None
        painter = self._open_painter() if owns_painter else self._active_painter
        if self._last_paint_pos is not None:
            painter.drawLine(self._last_paint_pos, scene_pos)
        else:
            painter.drawPoint(scene_pos)
        if owns_painter:
            painter.end()
        self._sync_display(force=owns_painter)  # one-shot calls always show immediately
        self._last_paint_pos = scene_pos

    # ---- select / copy / paste ------------------------------------------
    def has_selection(self):
        return self._selection_rect is not None and not self._selection_rect.isEmpty()

    def has_clipboard(self):
        return self._clipboard_image is not None

    def _clear_selection(self):
        self._selection_rect = None
        if self._selection_item is not None:
            self._scene.removeItem(self._selection_item)
            self._selection_item = None
        self.selection_changed.emit(False)

    def _image_bounds(self):
        if self.pixmap_item is None:
            return None
        return QRectF(self.pixmap_item.pixmap().rect())

    def _composite_image(self):
        """The base render + touch-up overlay flattened into one opaque
        QImage — what's actually visible right now. Copy always reads
        from this, not from the scene (which may also contain the
        selection-rectangle/paste-preview GUI items that must NOT end up
        in copied pixels)."""
        if self.pixmap_item is None:
            return None
        base = self.pixmap_item.pixmap()
        img = QImage(base.size(), QImage.Format_ARGB32_Premultiplied)
        painter = QPainter(img)
        painter.drawPixmap(0, 0, base)
        if self._retouch_image is not None:
            painter.drawImage(0, 0, self._retouch_image)
        painter.end()
        return img

    def copy_selection(self):
        """Copy the current selection's pixels (from the flattened
        base+touch-up composite) into the in-memory clipboard. Returns
        True on success, False if there's no valid selection."""
        bounds = self._image_bounds()
        if not self.has_selection() or bounds is None:
            return False
        rect = self._selection_rect.intersected(bounds).toRect()
        if rect.width() < 1 or rect.height() < 1:
            return False
        composite = self._composite_image()
        if composite is None:
            return False
        self._clipboard_image = composite.copy(rect)
        return True

    def arm_paste(self, return_mode="pan"):
        """Enter click-to-place paste mode: a semi-transparent preview of
        the clipboard follows the mouse; each left-click stamps a full
        copy at that spot (repeatable — doesn't auto-exit, since stamping
        out several instances of the same defect is a common case).
        Escape, right-click, or switching tool mode cancels/exits.
        return_mode: the tool mode to restore afterwards."""
        if self._clipboard_image is None or self.pixmap_item is None:
            return False
        self._paste_return_mode = return_mode
        self._paste_armed = True
        if self._paste_preview_item is not None:
            self._scene.removeItem(self._paste_preview_item)
        pix = QPixmap.fromImage(self._clipboard_image)
        self._paste_preview_item = self._scene.addPixmap(pix)
        self._paste_preview_item.setOpacity(0.6)
        self._paste_preview_item.setZValue(2)
        self._paste_preview_item.setOffset(-pix.width() / 2, -pix.height() / 2)
        self.viewport().setCursor(Qt.CrossCursor)
        self._report_mode()
        return True

    def _disarm_paste(self):
        self._paste_armed = False
        if self._paste_preview_item is not None:
            self._scene.removeItem(self._paste_preview_item)
            self._paste_preview_item = None

    def _commit_paste(self, scene_pos):
        if self._retouch_image is None or self._clipboard_image is None:
            return
        self._push_undo_snapshot()
        painter = QPainter(self._retouch_image)
        painter.setRenderHint(QPainter.Antialiasing, False)
        painter.setCompositionMode(QPainter.CompositionMode_Source)
        top_left = QPointF(scene_pos.x() - self._clipboard_image.width() / 2,
                            scene_pos.y() - self._clipboard_image.height() / 2)
        painter.drawImage(top_left, self._clipboard_image)
        painter.end()
        self._retouch_item.setPixmap(QPixmap.fromImage(self._retouch_image.copy()))
        self.retouch_changed.emit()

    # ---- mouse / keyboard events -----------------------------------------
    def mousePressEvent(self, event):
        pos = event.position().toPoint()
        if self._paste_armed:
            if event.button() == Qt.LeftButton:
                self._commit_paste(self.mapToScene(pos))
            else:
                self.set_tool_mode(self._paste_return_mode)  # right-click cancels
            event.accept()
            return
        if self._tool_mode == "paint" and event.button() == Qt.LeftButton and self.pixmap_item is not None:
            self._painting = True
            self._last_paint_pos = None
            self._begin_stroke()
            self._paint_at(self.mapToScene(pos))
            event.accept()
            return
        if self._tool_mode == "select" and event.button() == Qt.LeftButton and self.pixmap_item is not None:
            self._selecting = True
            self._select_start = self.mapToScene(pos)
            if self._selection_item is not None:
                self._scene.removeItem(self._selection_item)
            pen = QPen(QColor(30, 130, 255, 230))
            pen.setWidth(0)  # cosmetic: always 1 device pixel regardless of zoom
            pen.setStyle(Qt.DashLine)
            self._selection_item = self._scene.addRect(QRectF(self._select_start, self._select_start), pen)
            self._selection_item.setZValue(3)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        pos = event.position().toPoint()
        if self._paste_armed and self._paste_preview_item is not None:
            self._paste_preview_item.setPos(self.mapToScene(pos))
            event.accept()
            return
        if self._tool_mode == "paint" and self._painting:
            self._paint_at(self.mapToScene(pos))
            event.accept()
            return
        if self._tool_mode == "select" and self._selecting:
            rect = QRectF(self._select_start, self.mapToScene(pos)).normalized()
            self._selection_item.setRect(rect)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._paste_armed:
            event.accept()
            return
        if self._tool_mode == "paint" and self._painting and event.button() == Qt.LeftButton:
            self._painting = False
            self._end_stroke()
            self._last_paint_pos = None
            self.retouch_changed.emit()
            event.accept()
            return
        if self._tool_mode == "select" and self._selecting and event.button() == Qt.LeftButton:
            self._selecting = False
            bounds = self._image_bounds()
            rect = self._selection_item.rect()
            if bounds is not None:
                rect = rect.intersected(bounds)
            if rect.width() < 1 or rect.height() < 1:
                self._clear_selection()
            else:
                self._selection_rect = rect
                self._selection_item.setRect(rect)
                self.selection_changed.emit(True)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            if self._paste_armed:
                self.set_tool_mode(self._paste_return_mode)
                event.accept()
                return
            if self.has_selection():
                self._clear_selection()
                event.accept()
                return
        super().keyPressEvent(event)
