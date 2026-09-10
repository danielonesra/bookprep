import os
import shutil
import tempfile

import cv2
import numpy as np
from PySide6.QtCore import Qt, QRectF, QTimer, Signal, QSize, QThreadPool, QPoint
from PySide6.QtGui import QImage, QPixmap, QIcon, QAction
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QSplitter, QVBoxLayout, QHBoxLayout, QFormLayout,
    QListWidget, QListWidgetItem, QGraphicsScene, QLabel,
    QPushButton, QDoubleSpinBox, QSpinBox, QSlider, QCheckBox,
    QToolBar, QFileDialog, QMessageBox, QProgressDialog, QGroupBox,
    QAbstractItemView, QScrollArea, QStatusBar, QMenu, QInputDialog, QDialog,
    QApplication, QTabBar,
)

from project import Project, TRIM_SIZES_IN, DEFAULT_SETTINGS
import processing as proc
from crop_item import CropRectItem
from worker import Worker
from bw_dialog import BWOverrideDialog, build_slider, ComboBox
from move_page_dialog import MovePagesDialog
from autodetect_dialog import AutoDetectDialog
from size_dialog import SizeOverrideDialog
from zoom_view import ZoomableImageView, ZoomPanGraphicsView

INSERTABLE_FILTER = "PDF or images (*.pdf *.png *.jpg *.jpeg *.tif *.tiff)"


def numpy_to_qpixmap(img):
    """BGR (3-channel) or grayscale (1-channel) numpy uint8 array -> QPixmap."""
    if img.ndim == 2:
        h, w = img.shape
        qimg = QImage(img.data, w, h, w, QImage.Format_Grayscale8).copy()
    else:
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        h, w, _ = rgb.shape
        qimg = QImage(rgb.data, w, h, 3 * w, QImage.Format_RGB888).copy()
    return QPixmap.fromImage(qimg)


class ElasticSplitterPane(QWidget):
    """A plain QWidget except minimumSizeHint() always reports (0, 0),
    regardless of how wide its own layout/contents naturally want to be.

    This matters specifically for QSplitter panes: QSplitter's own drag/
    resize math uses widget.minimumSizeHint() (the value derived from the
    widget's layout), not the separate widget.minimumWidth()/
    setMinimumWidth() property -- so calling setMinimumWidth(0) on a
    normal QWidget has NO effect on how small a splitter will actually let
    it get. Without this override, a pane containing several rows of
    buttons/toolbars reports a minimumSizeHint() equal to the sum of that
    content's natural width, and QSplitter enforces that as a hard floor:
    dragging a handle elsewhere in the splitter, once that floor is hit,
    cascades into shrinking whichever OTHER pane it can, rather than the
    one under the floor -- which is exactly the "widening the right pane
    pushes the left pane instead, even though the middle pane still has
    visible room to close" symptom this class fixes. Content that gets
    squeezed smaller than it would like simply clips/wraps, same as any
    other under-sized widget -- there's no other downside."""
    def minimumSizeHint(self):
        return QSize(0, 0)


class PageListWidget(QListWidget):
    reordered = Signal()
    ctrl_wheel = Signal(int)  # angleDelta.y() when Ctrl is held, for thumbnail zoom

    def dropEvent(self, event):
        super().dropEvent(event)
        self.reordered.emit()

    def wheelEvent(self, event):
        if event.modifiers() & Qt.ControlModifier:
            self.ctrl_wheel.emit(event.angleDelta().y())
            event.accept()
        else:
            super().wheelEvent(event)  # normal scroll


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("BookPrep — scan-to-print")
        self.resize(1400, 900)

        self.project = Project()
        self.thumb_cache = {}
        self.thumb_width = 90
        self._pending_thumb_width = self.thumb_width
        self.THUMB_MIN, self.THUMB_MAX = 40, 240
        self.current_index = None
        self.crop_item = None
        self._current_raw_img = None  # keep a reference alive for QImage
        self._background_workers = []  # keep Worker/QRunnable objects alive
        # while queued/running -- see _run_worker()
        self._preview_retouch_page_uid = None  # which page's touch-up
        # layer is currently loaded into preview_view, so a settings-
        # triggered re-render (same page) doesn't reload/wipe in-progress
        # paint strokes -- only an actual page switch should reload it
        self._preview_content_rect = None  # (x, y, w, h) content-block
        # rect the CURRENTLY DISPLAYED preview render used -- see
        # processing.compose_page(..., return_rect=True). Compared
        # against each new render's rect so a margin/size-override edit
        # can (a) reposition the on-screen touch-up layer to match, and
        # (b) be recorded alongside the next saved mask so render_final()
        # can do the same repositioning later. See _on_preview_ready()
        # and on_retouch_changed().

        self.preview_timer = QTimer(self)
        self.preview_timer.setSingleShot(True)
        self.preview_timer.timeout.connect(self.refresh_preview)

        self.thumb_zoom_timer = QTimer(self)
        self.thumb_zoom_timer.setSingleShot(True)
        self.thumb_zoom_timer.timeout.connect(self._apply_thumb_zoom)

        self._build_toolbar()
        self._build_central()
        self.page_status_label = QLabel("—")
        self.statusBar().addPermanentWidget(self.page_status_label)
        self.statusBar().showMessage("Ready — add scanned PDFs to begin.")

    # ==================================================================
    # UI construction
    # ==================================================================
    def _build_toolbar(self):
        tb = QToolBar("Main")
        tb.setMovable(False)
        self.addToolBar(tb)

        def action(text, slot, tip=None):
            a = QAction(text, self)
            a.triggered.connect(slot)
            if tip:
                a.setToolTip(tip)
            tb.addAction(a)
            return a

        action("Add scans (PDF)…", self.add_scans, "Add one or more scanned PDF books")
        action("Add images…", self.add_images, "Add loose image pages")
        tb.addSeparator()
        action("Delete", self.delete_selected, "Delete selected page(s)")
        action("Duplicate", self.duplicate_selected, "Duplicate selected page(s)")
        action("Move page(s) to…", self.move_selected_to_dialog)
        tb.addSeparator()
        action("Auto-detect…", self.autodetect_dialog,
               "Auto-crop and/or auto-deskew all pages, or just a selection")
        tb.addSeparator()
        action("Save project…", self.save_project)
        action("Open project…", self.open_project)
        tb.addSeparator()
        action("Export PDF…", self.export_pdf, "Render and assemble the final print-ready PDF")
        action("Export PNGs (for GIMP)…", self.export_pngs_action,
               "Write every processed page as a sequential PNG for touch-up")
        action("Build PDF from PNG folder…", self.build_pdf_from_folder_action,
               "Assemble a PDF from a folder of (possibly touched-up) PNGs")

    def _build_central(self):
        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._build_page_rail())
        splitter.addWidget(self._build_editor())
        splitter.addWidget(self._build_settings_panel())
        splitter.setSizes([220, 850, 320])
        self.setCentralWidget(splitter)

    def _build_page_rail(self):
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(6, 6, 6, 6)
        # No visible "Pages (N)" title -- freed the row for Prev/Next
        # below instead. self.rail_label still exists (just not added to
        # any layout) so the existing .setText() calls that keep the
        # page count in it elsewhere don't need to change.
        self.rail_label = QLabel("Pages (0)")

        nav_row = QHBoxLayout()
        self.prev_btn = QPushButton("‹ Prev")
        self.prev_btn.clicked.connect(lambda: self.select_page(self.current_index - 1))
        self.next_btn = QPushButton("Next ›")
        self.next_btn.clicked.connect(lambda: self.select_page(self.current_index + 1))
        nav_row.addWidget(self.prev_btn)
        nav_row.addWidget(self.next_btn)
        layout.addLayout(nav_row)

        self.list_widget = PageListWidget()
        self.list_widget.setIconSize(self._thumb_icon_size())
        self.list_widget.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.list_widget.setDragDropMode(QAbstractItemView.InternalMove)
        self.list_widget.currentRowChanged.connect(self.select_page)
        self.list_widget.reordered.connect(self.on_list_reordered)
        self.list_widget.ctrl_wheel.connect(self.on_thumb_ctrl_wheel)
        self.list_widget.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list_widget.customContextMenuRequested.connect(self.show_page_context_menu)
        layout.addWidget(self.list_widget)
        return w

    def _build_zoom_row(self, view):
        """Build a −/percentage/+/Fit/100% zoom toolbar row. Pass the
        target view if it already exists to wire it up immediately,
        otherwise pass None and call _wire_zoom_row later."""
        row = QHBoxLayout()
        zoom_out_btn = QPushButton("−")
        zoom_out_btn.setFixedWidth(32)
        zoom_out_btn.setToolTip("Zoom out")
        zoom_in_btn = QPushButton("+")
        zoom_in_btn.setFixedWidth(32)
        zoom_in_btn.setToolTip("Zoom in")
        fit_btn = QPushButton("Fit")
        fit_btn.setToolTip("Fit the whole page in view")
        actual_btn = QPushButton("100%")
        actual_btn.setToolTip("Actual pixel size")
        pct_label = QLabel("100%")
        pct_label.setMinimumWidth(48)
        pct_label.setAlignment(Qt.AlignCenter)
        row.addWidget(QLabel("Zoom:"))
        row.addWidget(zoom_out_btn)
        row.addWidget(pct_label)
        row.addWidget(zoom_in_btn)
        row.addSpacing(10)
        row.addWidget(fit_btn)
        row.addWidget(actual_btn)
        row.addStretch()
        row.zoom_out_btn = zoom_out_btn
        row.zoom_in_btn = zoom_in_btn
        row.fit_btn = fit_btn
        row.actual_btn = actual_btn
        if view is not None:
            self._wire_zoom_row(row, view)
        return row, pct_label

    def _wire_zoom_row(self, row, view):
        row.zoom_out_btn.clicked.connect(lambda: view.zoom_out())
        row.zoom_in_btn.clicked.connect(lambda: view.zoom_in())
        row.fit_btn.clicked.connect(lambda: view.fit_to_window())
        row.actual_btn.clicked.connect(lambda: view.zoom_actual_size())

    def _build_editor(self):
        w = ElasticSplitterPane()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(0, 0, 0, 0)

        toolbar_row = QHBoxLayout()
        self.deskew_btn = QPushButton("Auto-deskew")
        self.deskew_btn.setToolTip("Automatically detect and correct text skew")
        self.deskew_btn.clicked.connect(self.autodetect_deskew_current)
        self.autodetect_btn = QPushButton("Auto-detect text block")
        self.autodetect_btn.clicked.connect(self.autodetect_current)
        self.reset_crop_btn = QPushButton("Reset crop")
        self.reset_crop_btn.setToolTip("Clear this page's crop region and make it croppable again")
        self.reset_crop_btn.clicked.connect(self.reset_crop)
        self.rotate_btn = QPushButton("⟳ Rotate 90°")
        self.rotate_btn.clicked.connect(self.rotate_current)
        self.ref_btn = QPushButton("Set as size reference")
        self.ref_btn.clicked.connect(self.set_reference)
        self.blank_page_btn = QPushButton("Blank page")
        self.blank_page_btn.setToolTip(
            "Mark this page as blank: no crop region and no thresholding at all, "
            "just a plain white page. Use this if the scan is genuinely blank/"
            "near-blank and cropping or thresholding is producing garbage.")
        self.blank_page_btn.clicked.connect(self.mark_page_blank)
        for btn in (self.deskew_btn, self.autodetect_btn, self.reset_crop_btn, self.rotate_btn,
                    self.ref_btn, self.blank_page_btn):
            toolbar_row.addWidget(btn)
        toolbar_row.addStretch()

        toolbar_row.addWidget(QLabel("Fine-tune deskew:"))
        self.deskew_spin = QDoubleSpinBox()
        self.deskew_spin.setRange(-15.0, 15.0)
        self.deskew_spin.setSingleStep(0.1)
        self.deskew_spin.setDecimals(1)
        self.deskew_spin.setSuffix("°")
        self.deskew_spin.valueChanged.connect(self.on_deskew_spin_changed)
        toolbar_row.addWidget(self.deskew_spin)
        layout.addLayout(toolbar_row)

        self.scene = QGraphicsScene()
        self.view = ZoomPanGraphicsView()
        self.view.setScene(self.scene)

        crop_view_container = ElasticSplitterPane()
        crop_view_layout = QVBoxLayout(crop_view_container)
        crop_view_layout.setContentsMargins(0, 0, 0, 0)
        crop_zoom_row, self.crop_zoom_pct_label = self._build_zoom_row(self.view)
        self.view.zoom_changed.connect(
            lambda z: self.crop_zoom_pct_label.setText(f"{z * 100:.0f}%"))
        crop_view_layout.addWidget(self.view)

        # Plain QWidget rather than a titled QGroupBox: the "Proof preview"
        # title bar was just wasted vertical space -- the splitter handle
        # between this and the crop view above is separator enough.
        preview_box = ElasticSplitterPane()
        preview_layout = QVBoxLayout(preview_box)
        preview_layout.setContentsMargins(0, 4, 0, 0)

        zoom_row, self.zoom_pct_label = self._build_zoom_row(None)  # view wired up below

        self.preview_view = ZoomableImageView()
        self.preview_view.setMinimumHeight(120)
        self.preview_view.zoom_changed.connect(
            lambda z: self.zoom_pct_label.setText(f"{z * 100:.0f}%"))
        self.preview_view.retouch_changed.connect(self.on_retouch_changed)
        self.preview_view.selection_changed.connect(self.on_selection_changed)
        self.preview_view.tool_mode_changed.connect(self.on_tool_mode_changed)
        preview_layout.addWidget(self.preview_view)
        self._wire_zoom_row(zoom_row, self.preview_view)

        touchup_row = QHBoxLayout()
        self.touchup_btn = QPushButton("✏ Touch-up mode")
        self.touchup_btn.setCheckable(True)
        self.touchup_btn.toggled.connect(self.on_touchup_mode_toggled)
        touchup_row.addWidget(self.touchup_btn)

        touchup_row.addWidget(QLabel("Brush:"))
        self.brush_size_slider, brush_size_widget = build_slider(1, 60, 8, "{:.0f}px", 1)
        self.brush_size_slider.valueChanged.connect(self.on_brush_size_changed)
        touchup_row.addWidget(brush_size_widget)

        self.paint_color_combo = ComboBox()
        self.paint_color_combo.addItem("Paint black", 0)
        self.paint_color_combo.addItem("Paint white", 255)
        self.paint_color_combo.currentIndexChanged.connect(self.on_paint_color_changed)
        touchup_row.addWidget(self.paint_color_combo)

        self.clear_touchup_btn = QPushButton("Clear touch-ups")
        self.clear_touchup_btn.clicked.connect(self.on_clear_touchup_clicked)
        touchup_row.addWidget(self.clear_touchup_btn)

        self.undo_btn = QPushButton("↶ Undo")
        self.undo_btn.setToolTip("Undo the last paint stroke, paste, or clear (Ctrl+Z)")
        self.undo_btn.setEnabled(False)
        self.undo_btn.clicked.connect(self.on_undo_clicked)
        self.preview_view.undo_available_changed.connect(self.undo_btn.setEnabled)
        touchup_row.addWidget(self.undo_btn)

        undo_action = QAction("Undo touch-up", self)
        undo_action.setShortcut("Ctrl+Z")
        undo_action.triggered.connect(self.on_undo_clicked)
        self.addAction(undo_action)

        touchup_row.addStretch(1)
        preview_layout.addLayout(touchup_row)

        select_row = QHBoxLayout()
        self.select_btn = QPushButton("⬚ Select region")
        self.select_btn.setCheckable(True)
        self.select_btn.toggled.connect(self.on_select_mode_toggled)
        select_row.addWidget(self.select_btn)

        self.copy_btn = QPushButton("Copy")
        self.copy_btn.setEnabled(False)
        self.copy_btn.clicked.connect(self.on_copy_clicked)
        select_row.addWidget(self.copy_btn)

        self.paste_btn = QPushButton("Paste")
        self.paste_btn.setEnabled(False)
        self.paste_btn.setCheckable(True)
        self.paste_btn.toggled.connect(self.on_paste_toggled)
        select_row.addWidget(self.paste_btn)

        paste_hint = QLabel("select a region, Copy, then Paste — click to stamp (repeatable, Esc/right-click to stop)")
        paste_hint.setStyleSheet("color: gray; font-size: 11px;")
        select_row.addWidget(paste_hint)
        select_row.addStretch(1)
        preview_layout.addLayout(select_row)

        self.on_touchup_mode_toggled(False)  # sync initial brush/color/drag-mode state

        self.crop_view_container = crop_view_container
        self.preview_box = preview_box

        self.editor_view_tabs = QTabBar()
        self.editor_view_tabs.addTab("Both")
        self.editor_view_tabs.addTab("Crop")
        self.editor_view_tabs.addTab("Proof")
        self.editor_view_tabs.setExpanding(False)
        self.editor_view_tabs.setDrawBase(False)
        self.editor_view_tabs.currentChanged.connect(self.on_editor_view_tab_changed)

        # Both zoom rows live here, next to the tabs, rather than each
        # sitting inside its own pane -- saves a row of vertical space in
        # both the crop and proof panes. Each is wrapped in its own
        # container so on_editor_view_tab_changed can show/hide it as a
        # unit to match whichever pane(s) are actually visible.
        self.crop_zoom_container = QWidget()
        self.crop_zoom_container.setLayout(crop_zoom_row)
        self.proof_zoom_container = QWidget()
        self.proof_zoom_container.setLayout(zoom_row)

        top_row = QHBoxLayout()
        top_row.addWidget(self.editor_view_tabs)
        top_row.addSpacing(16)
        top_row.addWidget(self.crop_zoom_container)
        top_row.addSpacing(16)
        top_row.addWidget(self.proof_zoom_container)
        top_row.addStretch(1)
        layout.addLayout(top_row)

        self.editor_splitter = QSplitter(Qt.Vertical)
        self.editor_splitter.addWidget(crop_view_container)
        self.editor_splitter.addWidget(preview_box)
        self.editor_splitter.setSizes([550, 350])
        self.editor_splitter.setStretchFactor(0, 1)
        self.editor_splitter.setStretchFactor(1, 1)
        layout.addWidget(self.editor_splitter, stretch=1)

        return w

    def _build_settings_panel(self):
        outer = QScrollArea()
        outer.setWidgetResizable(True)
        w = QWidget()
        outer.setWidget(w)
        layout = QVBoxLayout(w)

        # -- trim size -----------------------------------------------
        box = QGroupBox("Trim size")
        form = QFormLayout(box)
        self.trim_combo = ComboBox()
        for key, (tw, th) in TRIM_SIZES_IN.items():
            self.trim_combo.addItem(f"{tw} × {th} in", key)
        self.trim_combo.addItem("Custom…", "custom")
        self.trim_combo.setCurrentIndex(self.trim_combo.findData("6x9"))
        self.trim_combo.currentIndexChanged.connect(self.on_trim_combo_changed)
        form.addRow("Preset", self.trim_combo)

        custom_trim_row = QHBoxLayout()
        custom_trim_row.setContentsMargins(0, 0, 0, 0)
        self.custom_trim_w_spin = QDoubleSpinBox()
        self.custom_trim_w_spin.setRange(1.0, 50.0)
        self.custom_trim_w_spin.setSingleStep(0.05)
        self.custom_trim_w_spin.setDecimals(2)
        self.custom_trim_w_spin.setValue(6.0)
        self.custom_trim_w_spin.setSuffix(" in")
        self.custom_trim_h_spin = QDoubleSpinBox()
        self.custom_trim_h_spin.setRange(1.0, 50.0)
        self.custom_trim_h_spin.setSingleStep(0.05)
        self.custom_trim_h_spin.setDecimals(2)
        self.custom_trim_h_spin.setValue(9.0)
        self.custom_trim_h_spin.setSuffix(" in")
        self.custom_trim_w_spin.valueChanged.connect(self.on_settings_changed)
        self.custom_trim_h_spin.valueChanged.connect(self.on_settings_changed)
        custom_trim_row.addWidget(self.custom_trim_w_spin)
        custom_trim_row.addWidget(QLabel("×"))
        custom_trim_row.addWidget(self.custom_trim_h_spin)
        self._custom_trim_row_label = QLabel("Custom size")
        self._custom_trim_row_widget = QWidget()
        self._custom_trim_row_widget.setLayout(custom_trim_row)
        form.addRow(self._custom_trim_row_label, self._custom_trim_row_widget)
        self._update_trim_size_visibility()

        self.dpi_spin = QSpinBox()
        self.dpi_spin.setRange(150, 1200)
        self.dpi_spin.setSingleStep(50)
        self.dpi_spin.setValue(600)
        self.dpi_spin.valueChanged.connect(self.on_settings_changed)
        form.addRow("Output DPI", self.dpi_spin)
        layout.addWidget(box)

        # -- margins ---------------------------------------------------
        box = QGroupBox("Margins (inches)")
        form = QFormLayout(box)
        self.margin_top = self._spin(0.75)
        self.margin_bottom = self._spin(0.75)
        self.margin_inner = self._spin(0.85)
        self.margin_outer = self._spin(0.6)
        form.addRow("Top", self.margin_top)
        form.addRow("Bottom", self.margin_bottom)
        form.addRow("Inner (gutter)", self.margin_inner)
        form.addRow("Outer", self.margin_outer)
        self.mirror_check = QCheckBox("Mirror inner/outer on facing pages")
        self.mirror_check.setChecked(True)
        self.mirror_check.stateChanged.connect(self.on_settings_changed)
        form.addRow(self.mirror_check)
        self.align_combo = ComboBox()
        self.align_combo.addItem("Top", "top")
        self.align_combo.addItem("Center", "center")
        self.align_combo.addItem("Bottom", "bottom")
        self.align_combo.currentIndexChanged.connect(self.on_settings_changed)
        form.addRow("Vertical align", self.align_combo)
        layout.addWidget(box)

        # -- reference ---------------------------------------------------
        box = QGroupBox("Uniform sizing")
        v = QVBoxLayout(box)
        hint = QLabel("Set one clean page as the reference — every page's text "
                       "block is rescaled to match its physical width, so mixed-DPI "
                       "scans read as one consistent size.")
        hint.setWordWrap(True)
        v.addWidget(hint)
        self.ref_readout = QLabel("not set")
        self.ref_readout.setStyleSheet("color: #666; font-family: monospace;")
        v.addWidget(self.ref_readout)
        layout.addWidget(box)

        # -- B&W processing ---------------------------------------------
        box = QGroupBox("Black && white processing")
        form = QFormLayout(box)
        self.contrast_slider, contrast_row = build_slider(100, 250, 140, "{:.2f}", 100)
        self.contrast_slider.valueChanged.connect(self.on_settings_changed)
        form.addRow("Contrast", contrast_row)

        self.threshold_combo = ComboBox()
        self.threshold_combo.addItem("Adaptive (uneven lighting)", "adaptive")
        self.threshold_combo.addItem("Fixed", "fixed")
        self.threshold_combo.addItem("Otsu (global auto)", "otsu")
        self.threshold_combo.addItem("Sauvola (local mean+stddev)", "sauvola")
        self.threshold_combo.addItem("Wolf (Sauvola variant)", "wolf")
        self.threshold_combo.addItem("Fox (Wolf variant, bleed-through)", "fox")
        self.threshold_combo.addItem("Window (dynamic window)", "window")
        self.threshold_combo.addItem("Bradley (simple local mean)", "bradley")
        self.threshold_combo.addItem("Grad (gradient snip)", "grad")
        self.threshold_combo.addItem("EdgePlus (contrast prefilter)", "edgeplus")
        self.threshold_combo.addItem("BlurDiv (contrast prefilter)", "blurdiv")
        self.threshold_combo.addItem("EdgeDiv (EdgePlus+BlurDiv)", "edgediv")
        self.threshold_combo.setCurrentIndex(self.threshold_combo.findData(DEFAULT_SETTINGS["threshold_mode"]))
        self.threshold_combo.currentIndexChanged.connect(self.on_threshold_mode_changed)
        form.addRow("Threshold mode", self.threshold_combo)

        self.fixed_thresh_slider, fixed_row = build_slider(100, 240, 180, "{:.0f}", 1)
        self.fixed_thresh_slider.valueChanged.connect(self.on_settings_changed)
        self.fixed_thresh_row_label = QLabel("Fixed level")
        form.addRow(self.fixed_thresh_row_label, fixed_row)
        self.fixed_thresh_row_label.setVisible(False)
        fixed_row.setVisible(False)
        self._fixed_thresh_row_widget = fixed_row

        # -- controls shared by the ScanTailor-Advanced local methods --
        self.window_divisor_slider, window_divisor_row = build_slider(6, 40, 12, "{:.0f}", 1)
        self.window_divisor_slider.valueChanged.connect(self.on_settings_changed)
        self.window_divisor_label = QLabel("Window size (÷ page dim.)")
        form.addRow(self.window_divisor_label, window_divisor_row)
        self._advanced_rows = [(self.window_divisor_label, window_divisor_row)]

        self.binarize_k_slider, binarize_k_row = build_slider(0, 150, 34, "{:.2f}", 100)
        self.binarize_k_slider.valueChanged.connect(self.on_settings_changed)
        self.binarize_k_label = QLabel("Sensitivity (k)")
        form.addRow(self.binarize_k_label, binarize_k_row)
        self._advanced_rows.append((self.binarize_k_label, binarize_k_row))

        self.binarize_delta_slider, binarize_delta_row = build_slider(-30, 30, 0, "{:.0f}", 1)
        self.binarize_delta_slider.valueChanged.connect(self.on_settings_changed)
        self.binarize_delta_label = QLabel("Threshold shift")
        form.addRow(self.binarize_delta_label, binarize_delta_row)
        self._advanced_rows.append((self.binarize_delta_label, binarize_delta_row))

        self.binarize_bounds_slider, binarize_bounds_row = build_slider(0, 254, 1, "{:.0f}", 1)
        self.binarize_bounds_slider.valueChanged.connect(self.on_settings_changed)
        self.binarize_bounds_label = QLabel("Lower bound (pure-black cutoff)")
        form.addRow(self.binarize_bounds_label, binarize_bounds_row)
        self._bounds_rows = [(self.binarize_bounds_label, binarize_bounds_row)]

        self.savgol_check = QCheckBox("Savitzky-Golay smoothing before thresholding")
        self.savgol_check.setChecked(False)
        self.savgol_check.stateChanged.connect(self.on_settings_changed)
        form.addRow(self.savgol_check)
        self._advanced_rows.append((self.savgol_check, None))

        self.morph_smoothing_check = QCheckBox("Morphological smoothing after thresholding")
        self.morph_smoothing_check.setChecked(False)
        self.morph_smoothing_check.stateChanged.connect(self.on_settings_changed)
        form.addRow(self.morph_smoothing_check)
        self._advanced_rows.append((self.morph_smoothing_check, None))

        for label, row in self._advanced_rows:
            label.setVisible(False)
            if row is not None:
                row.setVisible(False)
        for label, row in self._bounds_rows:
            label.setVisible(False)
            row.setVisible(False)

        self.thicken_slider, thicken_row = build_slider(0, 4, 1, "{:.0f}px", 1)
        self.thicken_slider.valueChanged.connect(self.on_settings_changed)
        form.addRow("Thicken strokes", thicken_row)

        self.despeckle_combo = ComboBox()
        self.despeckle_combo.addItem("Off", "off")
        self.despeckle_combo.addItem("Cautious", "cautious")
        self.despeckle_combo.addItem("Normal (recommended)", "normal")
        self.despeckle_combo.addItem("Aggressive", "aggressive")
        self.despeckle_combo.setCurrentIndex(self.despeckle_combo.findData(DEFAULT_SETTINGS["despeckle_level"]))
        self.despeckle_combo.currentIndexChanged.connect(self.on_settings_changed)
        form.addRow("Remove scan speckle", self.despeckle_combo)

        bw_reset_btn = QPushButton("Reset to defaults")
        bw_reset_btn.clicked.connect(self.reset_bw_settings)
        form.addRow(bw_reset_btn)
        layout.addWidget(box)

        # -- auto-detect (crop) tuning ------------------------------------
        box = QGroupBox("Auto-detect (crop) tuning")
        v = QVBoxLayout(box)
        hint = QLabel("Controls how Auto-detect finds the body text block. "
                       "Affects future auto-detect runs only — already-set crop "
                       "boxes aren't changed retroactively.")
        hint.setWordWrap(True)
        v.addWidget(hint)
        form = QFormLayout()
        v.addLayout(form)

        self.detect_minarea_slider, minarea_row = build_slider(5, 500, 30, "{:.3f}%", 1000)
        self.detect_minarea_slider.valueChanged.connect(self.on_settings_changed)
        form.addRow("Ignore marks smaller than", minarea_row)

        self.detect_gap_slider, gap_row = build_slider(10, 80, 25, "{:.1f}%", 10)
        self.detect_gap_slider.valueChanged.connect(self.on_settings_changed)
        form.addRow("Line/paragraph gap tolerance", gap_row)

        self.detect_columngap_slider, columngap_row = build_slider(30, 250, 100, "{:.1f}%", 10)
        self.detect_columngap_slider.valueChanged.connect(self.on_settings_changed)
        form.addRow("Chapter-break / stray-mark\ndistance", columngap_row)

        self.detect_columnoverlap_slider, columnoverlap_row = build_slider(20, 80, 40, "{:.0f}%", 1)
        self.detect_columnoverlap_slider.valueChanged.connect(self.on_settings_changed)
        form.addRow("Column alignment strictness", columnoverlap_row)

        self.detect_pad_slider, pad_row = build_slider(0, 50, 10, "{:.1f}%", 10)
        self.detect_pad_slider.valueChanged.connect(self.on_settings_changed)
        form.addRow("Crop padding", pad_row)

        self.detect_threshold_combo = ComboBox()
        self.detect_threshold_combo.addItem("Otsu (global auto)", "otsu")
        self.detect_threshold_combo.addItem("Adaptive (uneven lighting)", "adaptive")
        self.detect_threshold_combo.addItem("Sauvola (local mean+stddev)", "sauvola")
        self.detect_threshold_combo.addItem("Wolf (Sauvola variant)", "wolf")
        self.detect_threshold_combo.addItem("Fox (Wolf variant, bleed-through)", "fox")
        self.detect_threshold_combo.addItem("Window (dynamic window)", "window")
        self.detect_threshold_combo.addItem("Bradley (simple local mean)", "bradley")
        self.detect_threshold_combo.addItem("Grad (gradient snip)", "grad")
        self.detect_threshold_combo.addItem("EdgePlus (contrast prefilter)", "edgeplus")
        self.detect_threshold_combo.addItem("BlurDiv (contrast prefilter)", "blurdiv")
        self.detect_threshold_combo.addItem("EdgeDiv (EdgePlus+BlurDiv)", "edgediv")
        self.detect_threshold_combo.setCurrentIndex(
            self.detect_threshold_combo.findData(DEFAULT_SETTINGS["detect_threshold_method"]))
        self.detect_threshold_combo.currentIndexChanged.connect(self.on_settings_changed)
        form.addRow("Threshold method", self.detect_threshold_combo)
        threshold_hint = QLabel(
            "The thresholding step Auto-detect uses internally to tell ink from "
            "page before it looks for the text block — same methods as the "
            "Black && white processing panel. If Otsu is misreading a scan's "
            "ink/paper split (e.g. it grabs the whole page, or grabs nothing), "
            "try whichever method works well for that scan's final B&&W output.")
        threshold_hint.setWordWrap(True)
        form.addRow(threshold_hint)

        self.detect_border_slider, border_row = build_slider(5, 450, 30, "{:.1f}%", 10)
        self.detect_border_slider.valueChanged.connect(self.on_settings_changed)
        form.addRow("Border/shadow strip width", border_row)
        border_hint = QLabel(
            "Raise this if a scan has a dark scanner-bed edge or binding/gutter "
            "shadow that's getting swept into the detected crop box (this is "
            "usually why Auto-detect grabs the whole page instead of just the "
            "text on scans that aren't already pre-cropped, clean B&&W scans).")
        border_hint.setWordWrap(True)
        form.addRow(border_hint)

        reset_btn = QPushButton("Reset to defaults")
        reset_btn.clicked.connect(self.reset_detect_settings)
        v.addWidget(reset_btn)
        layout.addWidget(box)

        for w_ in (self.margin_top, self.margin_bottom, self.margin_inner, self.margin_outer):
            w_.valueChanged.connect(self.on_settings_changed)

        layout.addStretch()
        return outer

    def _spin(self, value):
        s = QDoubleSpinBox()
        s.setRange(0, 3)
        s.setSingleStep(0.05)
        s.setValue(value)
        s.valueChanged.connect(self.on_settings_changed)
        return s

    # ==================================================================
    # page list <-> project sync
    # ==================================================================
    def get_thumb(self, page, force=False):
        if not force and page.uid in self.thumb_cache:
            return self.thumb_cache[page.uid]
        if page.is_blank:
            # Show what the page will actually look like (plain white),
            # not the raw scan -- render_final() ignores that entirely.
            s = self.project.settings
            aspect = (s["trim_h_in"] / s["trim_w_in"]) if s.get("trim_w_in") else 1.5
            h = max(1, int(self.thumb_width * aspect))
            blank = np.full((h, self.thumb_width), 255, dtype=np.uint8)
            pix = numpy_to_qpixmap(blank)
            self.thumb_cache[page.uid] = pix
            return pix
        img = self.project.get_raw_image(page)
        h, w = img.shape[:2]
        scale = self.thumb_width / w
        small = cv2.resize(img, (self.thumb_width, max(1, int(h * scale))), interpolation=cv2.INTER_AREA)
        pix = numpy_to_qpixmap(small)
        self.thumb_cache[page.uid] = pix
        return pix

    def _thumb_icon_size(self):
        return QSize(self.thumb_width, int(self.thumb_width * 1.5))

    def on_thumb_ctrl_wheel(self, angle_delta_y):
        step = 12 if angle_delta_y > 0 else -12
        self._pending_thumb_width = max(self.THUMB_MIN, min(self.THUMB_MAX, self._pending_thumb_width + step))
        self.thumb_zoom_timer.start(120)

    def _apply_thumb_zoom(self):
        if self._pending_thumb_width == self.thumb_width:
            return
        self.thumb_width = self._pending_thumb_width
        self.thumb_cache.clear()
        self.list_widget.setIconSize(self._thumb_icon_size())
        self.refresh_page_list()

    def _page_list_text(self, i, page):
        if page.is_blank:
            mark = "blank"
        else:
            mark = "done" if page.has_bbox() else "no crop"
        extra = []
        if not page.is_blank:
            if page.bw_disabled:
                extra.append("B&W off")
            elif page.bw_override:
                extra.append("custom B&W")
            if page.deskew_angle:
                extra.append(f"skew {page.deskew_angle:+.1f}°")
            if page.align_override:
                extra.append(f"align: {page.align_override}")
            if page.size_override:
                sv = page.size_override
                if sv.get("mode") == "absolute":
                    extra.append(f"size: {sv.get('value'):.2f}in")
                else:
                    extra.append(f"size: {sv.get('value', 1.0) * 100:.0f}%")
        tail = f" · {' · '.join(extra)}" if extra else ""
        return f"{i + 1}. {page.src_file}\np.{page.src_page + 1} · {page.src_dpi:.0f}dpi · {mark}{tail}"

    def refresh_page_list(self):
        self.rail_label.setText(f"Pages ({len(self.project.pages)})")
        self.list_widget.blockSignals(True)
        self.list_widget.clear()
        for i, page in enumerate(self.project.pages):
            pix = self.get_thumb(page)
            item = QListWidgetItem(QIcon(pix), self._page_list_text(i, page))
            item.setData(Qt.UserRole, page.uid)
            self.list_widget.addItem(item)
        self.list_widget.blockSignals(False)
        if self.current_index is not None and 0 <= self.current_index < len(self.project.pages):
            self.list_widget.setCurrentRow(self.current_index)

    def on_list_reordered(self):
        uid_order = [self.list_widget.item(i).data(Qt.UserRole) for i in range(self.list_widget.count())]
        self.project.reorder_by_uid(uid_order)
        self.current_index = self.list_widget.currentRow()
        self.refresh_page_list()

    # ==================================================================
    # page selection / crop editor
    # ==================================================================
    def select_page(self, index):
        if index is None or index < 0 or index >= len(self.project.pages):
            return
        self.current_index = index
        page = self.project.pages[index]

        self.reload_current_page_image()

        self.page_status_label.setText(
            f"Page {index + 1} / {len(self.project.pages)}  ·  {page.src_file} "
            f"p.{page.src_page + 1}  ·  {page.src_dpi:.0f} dpi  ·  rotation {page.rotation}°"
        )
        self.prev_btn.setEnabled(index > 0)
        self.next_btn.setEnabled(index < len(self.project.pages) - 1)
        if self.list_widget.currentRow() != index:
            self.list_widget.setCurrentRow(index)

        self.deskew_spin.blockSignals(True)
        self.deskew_spin.setValue(page.deskew_angle)
        self.deskew_spin.blockSignals(False)

    def reload_current_page_image(self):
        """Re-render the current page's raw image (after a rotation,
        deskew, or file-swap) and rebuild the crop overlay, preserving
        whatever crop box was already set."""
        if self.current_index is None:
            return
        page = self.project.pages[self.current_index]
        img = self.project.get_raw_image(page)
        self._current_raw_img = img
        pixmap = numpy_to_qpixmap(img)

        self.scene.clear()
        self.scene.addPixmap(pixmap)
        bounds = QRectF(0, 0, pixmap.width(), pixmap.height())
        self.scene.setSceneRect(bounds)
        self.view.set_fit_rect(bounds)

        bbox = page.active_bbox()
        if page.is_blank:
            # A blank page has no crop region at all -- don't offer a
            # draggable box over content that render_final() ignores
            # entirely anyway.
            self.crop_item = None
        else:
            rect = QRectF(*bbox) if bbox else QRectF(bounds)
            self.crop_item = CropRectItem(rect, bounds, on_change=self.on_crop_changed)
            self.scene.addItem(self.crop_item)
        self.refresh_preview()

    def on_crop_changed(self, rect: QRectF):
        if self.current_index is None:
            return
        page = self.project.pages[self.current_index]
        page.bbox = [round(rect.x()), round(rect.y()), round(rect.width()), round(rect.height())]
        self._update_current_list_item()
        self.schedule_preview_refresh()

    def _update_current_list_item(self):
        item = self.list_widget.item(self.current_index)
        if item is None:
            return
        page = self.project.pages[self.current_index]
        item.setText(self._page_list_text(self.current_index, page))

    # ---- toolbar actions on current page ------------------------------
    def autodetect_current(self):
        if self.current_index is None:
            return
        page = self.project.pages[self.current_index]
        self.project.autodetect(page)
        self.reload_current_page_image()
        self._update_current_list_item()

    def reset_crop(self):
        if self.current_index is None:
            return
        page = self.project.pages[self.current_index]
        page.bbox = None
        page.auto_bbox = None
        page.is_blank = False  # in case "Blank page" was clicked by accident
        self.get_thumb(page, force=True)
        self.reload_current_page_image()
        self._update_current_list_item()

    def mark_page_blank(self):
        if self.current_index is None:
            return
        page = self.project.pages[self.current_index]
        page.is_blank = True
        page.bbox = None
        page.auto_bbox = None
        page.deskew_angle = 0.0
        self.get_thumb(page, force=True)
        self.reload_current_page_image()
        self._update_current_list_item()

    def rotate_current(self):
        if self.current_index is None:
            return
        page = self.project.pages[self.current_index]
        self.project.rotate_page(page)
        self.get_thumb(page, force=True)
        self.select_page(self.current_index)
        self.refresh_page_list()

    def autodetect_deskew_current(self):
        if self.current_index is None:
            return
        page = self.project.pages[self.current_index]
        angle = self.project.autodetect_deskew(page)
        self.deskew_spin.blockSignals(True)
        self.deskew_spin.setValue(angle)
        self.deskew_spin.blockSignals(False)
        self.get_thumb(page, force=True)
        self.reload_current_page_image()
        self._update_current_list_item()

    def on_deskew_spin_changed(self, value):
        if self.current_index is None:
            return
        page = self.project.pages[self.current_index]
        page.deskew_angle = round(value, 1)
        self.get_thumb(page, force=True)
        self.reload_current_page_image()
        self._update_current_list_item()

    def set_reference(self):
        if self.current_index is None:
            return
        page = self.project.pages[self.current_index]
        try:
            width_in = self.project.set_reference_from(page)
        except ValueError as e:
            QMessageBox.warning(self, "No crop set", str(e))
            return
        self.ref_readout.setText(f"reference text width: {width_in:.3f} in (page {self.current_index + 1})")
        self.schedule_preview_refresh()

    def autodetect_dialog(self):
        if not self.project.pages:
            return
        selected_rows = self._selected_rows()
        dlg = AutoDetectDialog(self, len(self.project.pages), len(selected_rows))
        if dlg.exec() != QDialog.Accepted:
            return
        use_selected, do_crop, do_deskew = dlg.selection()
        pages = [self.project.pages[r] for r in selected_rows] if use_selected else list(self.project.pages)
        if not pages:
            return

        label = "Detecting…" if not (do_crop and do_deskew) else "Detecting and straightening…"
        progress = QProgressDialog(label, None, 0, len(pages), self)
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)

        def task(progress_cb=None):
            self.project.run_autodetect(pages, do_crop=do_crop, do_deskew=do_deskew, progress_cb=progress_cb)

        worker = Worker(task)
        worker.signals.progress.connect(lambda i, n: progress.setValue(i))
        worker.signals.finished.connect(lambda _: (progress.close(), self.refresh_page_list(),
                                                     self.select_page(self.current_index or 0)))
        worker.signals.error.connect(lambda msg: (progress.close(), QMessageBox.critical(self, "Error", msg)))
        self._run_worker(worker)

    # ==================================================================
    # page management: add / delete / duplicate / move
    # ==================================================================
    def _insert_index(self):
        return self.current_index + 1 if self.current_index is not None else None

    def _selected_rows(self):
        return sorted(set(i.row() for i in self.list_widget.selectedIndexes()))

    def add_scans(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Add scanned PDFs", "", "PDF files (*.pdf)")
        if not paths:
            return
        self._insert_files(paths, self._insert_index())

    def add_images(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Add image pages", "",
                                                 "Images (*.png *.jpg *.jpeg *.tif *.tiff)")
        if not paths:
            return
        self._insert_files(paths, self._insert_index())

    def add_page_before(self):
        rows = self._selected_rows()
        if len(rows) != 1:
            return
        paths, _ = QFileDialog.getOpenFileNames(self, "Add page(s) before this page", "", INSERTABLE_FILTER)
        if not paths:
            return
        self._insert_files(paths, rows[0])

    def add_page_after(self):
        rows = self._selected_rows()
        if len(rows) != 1:
            return
        paths, _ = QFileDialog.getOpenFileNames(self, "Add page(s) after this page", "", INSERTABLE_FILTER)
        if not paths:
            return
        self._insert_files(paths, rows[0] + 1)

    def add_pdf_before(self):
        rows = self._selected_rows()
        if len(rows) != 1:
            return
        paths, _ = QFileDialog.getOpenFileNames(self, "Add PDF before this page", "", "PDF files (*.pdf)")
        if not paths:
            return
        self._insert_files(paths, rows[0])

    def add_pdf_after(self):
        rows = self._selected_rows()
        if len(rows) != 1:
            return
        paths, _ = QFileDialog.getOpenFileNames(self, "Add PDF after this page", "", "PDF files (*.pdf)")
        if not paths:
            return
        self._insert_files(paths, rows[0] + 1)

    def _insert_files(self, paths, insert_at):
        """Insert a mix of PDFs and/or images at `insert_at` (None = append).
        PDF rendering runs on a background thread since it can be slow —
        and, for the PDF case, the actual page-by-page rendering happens
        in an isolated subprocess (see pdf_render_worker.py); progress is
        reported back live so a long book shows real per-page progress
        instead of an indeterminate spinner."""
        progress = QProgressDialog("Adding pages…", None, 0, 0, self)
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.show()

        def task(progress_cb=None):
            running = insert_at
            for p in paths:
                if p.lower().endswith(".pdf"):
                    new_pages = self.project.add_pdf(p, insert_at=running, progress_cb=progress_cb)
                    count = len(new_pages)
                else:
                    self.project.add_image(p, insert_at=running)
                    count = 1
                if running is not None:
                    running += count

        worker = Worker(task)

        def on_progress(i, n):
            if progress.maximum() != n:
                progress.setRange(0, n)
            progress.setValue(i)
            progress.setLabelText(f"Rendering page {i} of {n}…")

        worker.signals.progress.connect(on_progress)

        def done(_):
            progress.close()
            self.refresh_page_list()
            if self.current_index is None and self.project.pages:
                self.select_page(0)
            self.statusBar().showMessage(f"Project now has {len(self.project.pages)} pages.", 4000)

        worker.signals.finished.connect(done)
        worker.signals.error.connect(lambda msg: (progress.close(), QMessageBox.critical(self, "Error adding pages", msg)))
        self._run_worker(worker)

    def delete_selected(self):
        rows = sorted(set(i.row() for i in self.list_widget.selectedIndexes()))
        if not rows:
            return
        if QMessageBox.question(self, "Delete pages", f"Delete {len(rows)} page(s)? This cannot be undone.",
                                 QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        uids = [self.project.pages[r].uid for r in rows]
        self.project.delete_pages(rows)
        for u in uids:
            self.thumb_cache.pop(u, None)
        self.current_index = None
        self.refresh_page_list()
        if self.project.pages:
            new_row = min(rows[0], len(self.project.pages) - 1)
            self.select_page(new_row)
        else:
            self.scene.clear()
            self.preview_view.clear()
            self.page_status_label.setText("—")

    def duplicate_selected(self):
        rows = sorted(set(i.row() for i in self.list_widget.selectedIndexes()))
        if not rows:
            return
        offset = 0
        for r in rows:
            self.project.duplicate_page(r + offset)
            offset += 1
        self.refresh_page_list()

    def move_selected_to_dialog(self):
        rows = self._selected_rows()
        if not rows:
            return
        dlg = MovePagesDialog(self, len(self.project.pages), rows)
        if dlg.exec() != QDialog.Accepted:
            return
        anchor_row, position = dlg.anchor_row_and_position()
        anchor_page = self.project.pages[anchor_row]
        # move_pages() wants a target index into the list *after* the
        # moving pages are removed -- so find where the anchor page (a
        # specific page, tracked by identity, not by its current index)
        # ends up once that removal happens, then offset by one for
        # "after".
        rows_set = set(rows)
        remaining = [p for i, p in enumerate(self.project.pages) if i not in rows_set]
        anchor_pos = remaining.index(anchor_page)
        target_index = anchor_pos + 1 if position == "after" else anchor_pos
        self.project.move_pages(rows, target_index)
        self.refresh_page_list()
        self.current_index = target_index
        self.select_page(target_index)

    # ---- bulk operations on the current selection ----------------------
    def rotate_selected(self):
        rows = self._selected_rows()
        if not rows:
            return
        for r in rows:
            page = self.project.pages[r]
            self.project.rotate_page(page)
            self.get_thumb(page, force=True)
        self.refresh_page_list()
        if self.current_index in rows:
            self.select_page(self.current_index)

    def reset_crop_selected(self):
        rows = self._selected_rows()
        if not rows:
            return
        for r in rows:
            page = self.project.pages[r]
            page.bbox = None
            page.auto_bbox = None
            page.is_blank = False  # in case "Blank page" was clicked by accident
        self.refresh_page_list()
        if self.current_index in rows:
            self.reload_current_page_image()

    def autodetect_selected(self):
        rows = self._selected_rows()
        if not rows:
            return
        progress = QProgressDialog("Detecting text blocks…", None, 0, len(rows), self)
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)

        def task(progress_cb=None):
            for i, r in enumerate(rows):
                self.project.autodetect(self.project.pages[r])
                if progress_cb:
                    progress_cb(i + 1, len(rows))

        worker = Worker(task)
        worker.signals.progress.connect(lambda i, n: progress.setValue(i))

        def done(_):
            progress.close()
            self.refresh_page_list()
            if self.current_index in rows:
                self.reload_current_page_image()

        worker.signals.finished.connect(done)
        worker.signals.error.connect(lambda msg: (progress.close(), QMessageBox.critical(self, "Error", msg)))
        self._run_worker(worker)

    def deskew_selected(self):
        rows = self._selected_rows()
        if not rows:
            return
        progress = QProgressDialog("Detecting skew…", None, 0, len(rows), self)
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)

        def task(progress_cb=None):
            for i, r in enumerate(rows):
                page = self.project.pages[r]
                self.project.autodetect_deskew(page)
                if progress_cb:
                    progress_cb(i + 1, len(rows))

        worker = Worker(task)
        worker.signals.progress.connect(lambda i, n: progress.setValue(i))

        def done(_):
            progress.close()
            for r in rows:
                self.get_thumb(self.project.pages[r], force=True)
            self.refresh_page_list()
            if self.current_index in rows:
                self.deskew_spin.blockSignals(True)
                self.deskew_spin.setValue(self.project.pages[self.current_index].deskew_angle)
                self.deskew_spin.blockSignals(False)
                self.reload_current_page_image()

        worker.signals.finished.connect(done)
        worker.signals.error.connect(lambda msg: (progress.close(), QMessageBox.critical(self, "Error", msg)))
        self._run_worker(worker)

    def set_deskew_selected_dialog(self):
        rows = self._selected_rows()
        if not rows:
            return
        current = self.project.pages[rows[0]].deskew_angle
        angle, ok = QInputDialog.getDouble(
            self, "Set deskew angle", "Angle in degrees (+/- 15, tenths allowed):",
            current, -15.0, 15.0, 1)
        if not ok:
            return
        for r in rows:
            page = self.project.pages[r]
            page.deskew_angle = round(angle, 1)
            self.get_thumb(page, force=True)
        self.refresh_page_list()
        if self.current_index in rows:
            self.deskew_spin.blockSignals(True)
            self.deskew_spin.setValue(round(angle, 1))
            self.deskew_spin.blockSignals(False)
            self.reload_current_page_image()

    def open_bw_override_dialog(self):
        rows = self._selected_rows()
        if not rows:
            return
        first_page = self.project.pages[rows[0]]
        initial = first_page.bw_override or self.project.settings
        dlg = BWOverrideDialog(self, initial, len(rows), initial_disabled=first_page.bw_disabled)
        if dlg.exec() != QDialog.Accepted:
            return
        pages = [self.project.pages[r] for r in rows]
        if dlg.cleared:
            self.project.set_bw_disabled(pages, False)
            self.project.set_bw_override(pages, None)
        elif dlg.disabled:
            self.project.set_bw_disabled(pages, True)
        else:
            self.project.set_bw_disabled(pages, False)
            self.project.set_bw_override(pages, dlg.result_override())
        self.refresh_page_list()
        if self.current_index in rows:
            self.schedule_preview_refresh()

    def clear_bw_override_selected(self):
        rows = self._selected_rows()
        if not rows:
            return
        pages = [self.project.pages[r] for r in rows]
        self.project.set_bw_disabled(pages, False)
        self.project.set_bw_override(pages, None)
        self.refresh_page_list()
        if self.current_index in rows:
            self.schedule_preview_refresh()

    def open_size_override_dialog(self):
        rows = self._selected_rows()
        if not rows:
            return
        first_page = self.project.pages[rows[0]]
        dlg = SizeOverrideDialog(self, first_page.size_override, len(rows))
        if dlg.exec() != QDialog.Accepted:
            return
        pages = [self.project.pages[r] for r in rows]
        self.project.set_size_override(pages, dlg.result_override())
        self.refresh_page_list()
        if self.current_index in rows:
            self.schedule_preview_refresh()

    def clear_size_override_selected(self):
        rows = self._selected_rows()
        if not rows:
            return
        pages = [self.project.pages[r] for r in rows]
        self.project.set_size_override(pages, None)
        self.refresh_page_list()
        if self.current_index in rows:
            self.schedule_preview_refresh()

    # ---- right-click context menu ---------------------------------------
    def _popup_menu_on_screen(self, menu, global_pos):
        """Show `menu` at `global_pos`, but nudged back on-screen if it
        would otherwise extend past the bottom or right edge of the
        display — this matters for menus tall enough to overflow when
        triggered near the bottom of a scrollable list.

        Two passes, not one: an initial best-guess position from
        sizeHint() (cheap, avoids visible jump in the common case), then
        a correction after the menu has actually been shown and laid
        out, using its real geometry(). sizeHint() measured before a
        QMenu has ever been shown is not always reliable — it can
        underestimate the real rendered size, especially under
        non-standard DPI scaling — so relying on it alone isn't enough
        to guarantee no overflow; the second pass fixes that regardless
        of *why* the estimate was off."""
        screen = QApplication.screenAt(global_pos) or self.screen()
        geo = screen.availableGeometry() if screen else None

        def _clamp_into(rect_size, pos):
            if geo is None:
                return pos
            x = min(pos.x(), geo.x() + geo.width() - rect_size.width())
            y = min(pos.y(), geo.y() + geo.height() - rect_size.height())
            x = max(x, geo.x())
            y = max(y, geo.y())
            return QPoint(x, y)

        global_pos = _clamp_into(menu.sizeHint(), global_pos)
        menu.popup(global_pos)

        def _fix_after_show():
            real = menu.geometry()
            corrected = _clamp_into(real.size(), real.topLeft())
            if corrected != real.topLeft():
                menu.move(corrected)

        QTimer.singleShot(0, _fix_after_show)

    def show_page_context_menu(self, pos):
        rows = self._selected_rows()
        if not rows:
            return
        menu = QMenu(self)

        menu.addAction("Delete", self.delete_selected)
        menu.addAction("Duplicate", self.duplicate_selected)
        menu.addAction("Move page(s) to…", self.move_selected_to_dialog)

        if len(rows) == 1:
            menu.addSeparator()
            menu.addAction("Add page before…", self.add_page_before)
            menu.addAction("Add page after…", self.add_page_after)
            menu.addAction("Add pdf before…", self.add_pdf_before)
            menu.addAction("Add pdf after…", self.add_pdf_after)

        menu.addSeparator()
        menu.addAction("Auto-detect text block", self.autodetect_selected)
        menu.addAction("Reset crop", self.reset_crop_selected)
        menu.addAction("Rotate 90°", self.rotate_selected)

        menu.addSeparator()
        menu.addAction("Auto-deskew", self.deskew_selected)
        menu.addAction("Set deskew angle…", self.set_deskew_selected_dialog)

        menu.addSeparator()
        menu.addAction("Black && white processing…", self.open_bw_override_dialog)
        if any(self.project.pages[r].bw_override or self.project.pages[r].bw_disabled for r in rows):
            menu.addAction("Clear black && white override", self.clear_bw_override_selected)

        menu.addSeparator()
        align_menu = menu.addMenu("Vertical alignment")
        current_overrides = {self.project.pages[r].align_override for r in rows}
        common_override = current_overrides.pop() if len(current_overrides) == 1 else None
        for label, value in (("Top", "top"), ("Center", "center"), ("Bottom", "bottom")):
            a = align_menu.addAction(label, lambda v=value: self.set_align_selected(v))
            a.setCheckable(True)
            a.setChecked(common_override == value)
        align_menu.addSeparator()
        default_action = align_menu.addAction(
            f"Use global default (currently: {self.project.settings['align'].capitalize()})",
            lambda: self.set_align_selected(None))
        default_action.setCheckable(True)
        default_action.setChecked(common_override is None)

        menu.addSeparator()
        menu.addAction("Page size…", self.open_size_override_dialog)
        if any(self.project.pages[r].size_override for r in rows):
            menu.addAction("Clear page size override", self.clear_size_override_selected)

        self._popup_menu_on_screen(menu, self.list_widget.viewport().mapToGlobal(pos))

    def set_align_selected(self, align):
        rows = self._selected_rows()
        if not rows:
            return
        pages = [self.project.pages[r] for r in rows]
        self.project.set_align_override(pages, align)
        self.refresh_page_list()
        if self.current_index in rows:
            self.schedule_preview_refresh()

    # ==================================================================
    # settings -> project.settings, debounced preview
    # ==================================================================
    def on_trim_combo_changed(self):
        self._update_trim_size_visibility()
        self.on_settings_changed()

    def _update_trim_size_visibility(self):
        """Show/hide the custom width/height spinboxes to match whether
        self.trim_combo is currently set to "Custom...". Split out from
        on_trim_combo_changed() so apply_settings_to_form() can call
        just this (UI-only) half without also triggering
        on_settings_changed()'s settings-dict write while the form is
        still mid-sync."""
        is_custom = self.trim_combo.currentData() == "custom"
        self._custom_trim_row_label.setVisible(is_custom)
        self._custom_trim_row_widget.setVisible(is_custom)

    def on_threshold_mode_changed(self):
        self._update_threshold_mode_visibility()
        self.on_settings_changed()

    def _update_threshold_mode_visibility(self):
        """Show/hide the threshold-mode-specific rows to match
        self.threshold_combo's current value. Split out from
        on_threshold_mode_changed() so apply_settings_to_form() can call
        just this (UI-only) half without also triggering
        on_settings_changed()'s settings-dict write while the form is
        still mid-sync -- see apply_settings_to_form() for why that
        matters."""
        mode = self.threshold_combo.currentData()
        is_fixed = mode == "fixed"
        self.fixed_thresh_row_label.setVisible(is_fixed)
        self._fixed_thresh_row_widget.setVisible(is_fixed)

        is_advanced = mode not in ("adaptive", "fixed", "otsu")
        for label, row in self._advanced_rows:
            label.setVisible(is_advanced)
            if row is not None:
                row.setVisible(is_advanced)

        needs_bounds = mode in ("wolf", "fox", "window", "grad")
        for label, row in self._bounds_rows:
            label.setVisible(is_advanced and needs_bounds)
            row.setVisible(is_advanced and needs_bounds)

    def on_settings_changed(self, *_):
        s = self.project.settings
        key = self.trim_combo.currentData()
        if key == "custom":
            tw, th = self.custom_trim_w_spin.value(), self.custom_trim_h_spin.value()
        else:
            tw, th = TRIM_SIZES_IN[key]
        s.update({
            "trim_preset": key, "trim_w_in": tw, "trim_h_in": th,
            "dpi": self.dpi_spin.value(),
            "margin_top_in": self.margin_top.value(),
            "margin_bottom_in": self.margin_bottom.value(),
            "margin_inner_in": self.margin_inner.value(),
            "margin_outer_in": self.margin_outer.value(),
            "mirror_margins": self.mirror_check.isChecked(),
            "align": self.align_combo.currentData(),
            "contrast": self.contrast_slider.value() / 100.0,
            "threshold_mode": self.threshold_combo.currentData(),
            "fixed_threshold": self.fixed_thresh_slider.value(),
            "window_divisor": self.window_divisor_slider.value(),
            "binarize_k": self.binarize_k_slider.value() / 100.0,
            "binarize_delta": float(self.binarize_delta_slider.value()),
            "binarize_lower": self.binarize_bounds_slider.value(),
            "savgol_enabled": self.savgol_check.isChecked(),
            "morph_smoothing": self.morph_smoothing_check.isChecked(),
            "thicken_px": self.thicken_slider.value(),
            "despeckle_level": self.despeckle_combo.currentData(),
            "detect_min_area_frac": self.detect_minarea_slider.value() / 100000.0,
            "detect_cluster_gap_frac": self.detect_gap_slider.value() / 1000.0,
            "detect_column_vgap_frac": self.detect_columngap_slider.value() / 1000.0,
            "detect_column_overlap_frac": self.detect_columnoverlap_slider.value() / 100.0,
            "detect_pad_frac": self.detect_pad_slider.value() / 1000.0,
            "detect_border_strip_frac": self.detect_border_slider.value() / 1000.0,
            "detect_threshold_method": self.detect_threshold_combo.currentData(),
        })
        self.schedule_preview_refresh()

    def reset_bw_settings(self):
        d = DEFAULT_SETTINGS
        self.contrast_slider.setValue(int(round(d["contrast"] * 100)))
        self.threshold_combo.setCurrentIndex(self.threshold_combo.findData(d["threshold_mode"]))
        self.fixed_thresh_slider.setValue(d["fixed_threshold"])
        self.window_divisor_slider.setValue(d["window_divisor"])
        self.binarize_k_slider.setValue(int(round(d["binarize_k"] * 100)))
        self.binarize_delta_slider.setValue(int(round(d["binarize_delta"])))
        self.binarize_bounds_slider.setValue(d["binarize_lower"])
        self.savgol_check.setChecked(d["savgol_enabled"])
        self.morph_smoothing_check.setChecked(d["morph_smoothing"])
        self.thicken_slider.setValue(d["thicken_px"])
        self.despeckle_combo.setCurrentIndex(self.despeckle_combo.findData(d["despeckle_level"]))
        self.on_settings_changed()  # setValue()/setCurrentIndex() may not fire if already at that value

    def reset_detect_settings(self):
        d = DEFAULT_SETTINGS
        self.detect_minarea_slider.setValue(round(d["detect_min_area_frac"] * 100000))
        self.detect_gap_slider.setValue(round(d["detect_cluster_gap_frac"] * 1000))
        self.detect_columngap_slider.setValue(round(d["detect_column_vgap_frac"] * 1000))
        self.detect_columnoverlap_slider.setValue(round(d["detect_column_overlap_frac"] * 100))
        self.detect_pad_slider.setValue(round(d["detect_pad_frac"] * 1000))
        self.detect_threshold_combo.setCurrentIndex(
            self.detect_threshold_combo.findData(d["detect_threshold_method"]))
        self.detect_border_slider.setValue(round(d["detect_border_strip_frac"] * 1000))
        self.on_settings_changed()  # setValue() may not fire if a slider's value is unchanged

    def _run_worker(self, worker):
        """
        Start a Worker on the global QThreadPool while keeping a strong
        Python reference to it until it's done.

        Without this, `worker` (a QRunnable with no QObject parent) can be
        garbage-collected the moment the caller's local scope ends -- the
        C++/QThreadPool side may still run it, but its `finished`/`error`
        signals can silently fail to reach their connected slots, so
        nothing ever seems to happen (no crash, no error message either).
        This was a real, hard-to-spot bug: the preview pane simply never
        updated after changing B&W settings, because its worker's
        `finished` signal was getting lost this way.
        """
        self._background_workers.append(worker)

        def _cleanup(*_):
            try:
                self._background_workers.remove(worker)
            except ValueError:
                pass
        worker.signals.finished.connect(_cleanup)
        worker.signals.error.connect(_cleanup)

        from PySide6.QtCore import QThreadPool
        QThreadPool.globalInstance().start(worker)

    def schedule_preview_refresh(self):
        self.preview_timer.start(250)

    def refresh_preview(self):
        if self.current_index is None or not self.project.pages:
            return
        idx = self.current_index
        page = self.project.pages[idx]

        def task():
            img, rect = self.project.render_final(page, index_in_book=idx, also_return_rect=True)
            return idx, img, rect

        worker = Worker(task)
        worker.signals.finished.connect(self._on_preview_ready)
        worker.signals.error.connect(lambda msg: self.statusBar().showMessage(f"Preview error: {msg}", 4000))
        self._run_worker(worker)

    def on_editor_view_tab_changed(self, index):
        # index 0 = Both, 1 = Crop, 2 = Proof
        show_crop = index in (0, 1)
        show_proof = index in (0, 2)
        self.crop_view_container.setVisible(show_crop)
        self.preview_box.setVisible(show_proof)
        self.crop_zoom_container.setVisible(show_crop)
        self.proof_zoom_container.setVisible(show_proof)

    def on_touchup_mode_toggled(self, checked):
        self.brush_size_slider.setEnabled(checked)
        self.paint_color_combo.setEnabled(checked)
        self.preview_view.set_brush_size(self.brush_size_slider.value())
        self.preview_view.set_paint_color(self.paint_color_combo.currentData())
        if checked:
            self.preview_view.set_tool_mode("paint")
        elif self.preview_view.tool_mode == "paint":
            self.preview_view.set_tool_mode("pan")

    def on_brush_size_changed(self, value):
        self.preview_view.set_brush_size(value)

    def on_paint_color_changed(self):
        self.preview_view.set_paint_color(self.paint_color_combo.currentData())

    def on_clear_touchup_clicked(self):
        if self.current_index is None:
            return
        reply = QMessageBox.question(
            self, "Clear touch-ups",
            "Remove all manual paint touch-ups on this page? "
            "(You can still Undo this afterward.)",
            QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel)
        if reply == QMessageBox.Yes:
            self.preview_view.clear_retouch()  # emits retouch_changed -> persisted below

    def on_undo_clicked(self):
        if not self.preview_view.undo():
            self.statusBar().showMessage("Nothing to undo.", 3000)

    def on_retouch_changed(self):
        if self.current_index is None:
            return
        page = self.project.pages[self.current_index]
        mask = self.preview_view.get_retouch_mask()
        self.project.save_retouch_mask(page, mask, content_rect=self._preview_content_rect)

    def on_select_mode_toggled(self, checked):
        if checked:
            self.preview_view.set_tool_mode("select")
        elif self.preview_view.tool_mode == "select":
            self.preview_view.set_tool_mode("pan")

    def on_selection_changed(self, has_selection):
        self.copy_btn.setEnabled(has_selection)

    def on_copy_clicked(self):
        if self.preview_view.copy_selection():
            self.paste_btn.setEnabled(True)
        else:
            self.statusBar().showMessage("Select a region first.", 3000)

    def on_paste_toggled(self, checked):
        if checked:
            # return to whichever real tool was active before pasting
            return_mode = self.preview_view.tool_mode if self.preview_view.tool_mode != "pan" else "select"
            if not self.preview_view.arm_paste(return_mode=return_mode):
                self.paste_btn.setChecked(False)
        elif self.preview_view.is_paste_armed:
            self.preview_view.set_tool_mode(self.preview_view.tool_mode)

    def on_tool_mode_changed(self, mode):
        """Keep the toolbar buttons' checked state in sync with the
        view's actual mode, including when the view exits paste mode on
        its own (Escape / right-click) rather than via the Paste button."""
        for btn, is_checked in [
            (self.touchup_btn, mode == "paint"),
            (self.select_btn, mode == "select"),
            (self.paste_btn, mode == "paste"),
        ]:
            if btn.isChecked() != is_checked:
                btn.blockSignals(True)
                btn.setChecked(is_checked)
                btn.blockSignals(False)

    def _on_preview_ready(self, result):
        idx, img, content_rect = result
        if idx != self.current_index:
            return  # stale — user already moved on
        pix = numpy_to_qpixmap(img)
        self.preview_view.set_pixmap(pix)
        page = self.project.pages[idx]
        if page.uid != self._preview_retouch_page_uid:
            # Actual page switch (not just a settings-triggered re-render
            # of the same page) -- safe to (re)load the saved touch-up
            # layer without risking wiping out in-progress paint strokes.
            #
            # The saved mask lines up with whatever content rect was in
            # effect when it was last saved (see get_retouch_rect()) --
            # which may not be the current one, e.g. margins were
            # changed since (possibly in an earlier session). The base
            # image `img` already has this handled correctly, since
            # render_final() does the same remap before baking the
            # retouch into it -- but this overlay is a SEPARATE layer
            # drawn on top of that base image for interactive painting,
            # and loading it unremapped would stack stale, wrongly
            # positioned marks on top of an already-correct render.
            mask = self.project.get_retouch_mask(page)
            if mask is not None:
                saved_rect = self.project.get_retouch_rect(page)
                mask = proc.remap_retouch_mask(mask, saved_rect, content_rect, img.shape)
            self.preview_view.load_retouch_mask(mask)
            self._preview_retouch_page_uid = page.uid
        elif self._preview_content_rect is not None and content_rect != self._preview_content_rect:
            # Same page, but a margin (or size-override) edit moved/
            # rescaled where the content sits on the canvas since the
            # last render. Shift the in-progress touch-up layer along
            # with it -- via set_retouch_pixels(), which (unlike
            # load_retouch_mask()) doesn't reset undo history -- so a
            # stroke painted over a specific spot in the content stays
            # over that spot instead of staying at a fixed page position
            # while the content slides out from under it.
            mask = self.preview_view.get_retouch_mask()
            if mask is not None:
                remapped = proc.remap_retouch_mask(mask, self._preview_content_rect, content_rect, img.shape)
                self.preview_view.set_retouch_pixels(remapped)
        self._preview_content_rect = content_rect

    # ==================================================================
    # export
    # ==================================================================
    def export_pdf(self):
        if not self.project.pages:
            QMessageBox.information(self, "Nothing to export", "Add some pages first.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export print-ready PDF", "final_book.pdf", "PDF files (*.pdf)")
        if not path:
            return
        progress = QProgressDialog("Rendering final pages…", None, 0, len(self.project.pages), self)
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)

        def task(progress_cb=None):
            return self.project.export_pdf(path, progress_cb=progress_cb)

        worker = Worker(task)
        worker.signals.progress.connect(lambda i, n: progress.setValue(i))
        worker.signals.finished.connect(lambda p: (progress.close(),
                                                     QMessageBox.information(self, "Export complete", f"Saved to:\n{p}")))
        worker.signals.error.connect(lambda msg: (progress.close(), QMessageBox.critical(self, "Export failed", msg)))
        self._run_worker(worker)

    def export_pngs_action(self):
        if not self.project.pages:
            QMessageBox.information(self, "Nothing to export", "Add some pages first.")
            return
        folder = QFileDialog.getExistingDirectory(self, "Choose folder for the PNG page sequence")
        if not folder:
            return
        progress = QProgressDialog("Rendering pages to PNG…", None, 0, len(self.project.pages), self)
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)

        def task(progress_cb=None):
            return self.project.export_pngs(folder, progress_cb=progress_cb)

        worker = Worker(task)
        worker.signals.progress.connect(lambda i, n: progress.setValue(i))

        def done(paths):
            progress.close()
            QMessageBox.information(
                self, "Export complete",
                f"Wrote {len(paths)} sequential PNGs to:\n{folder}\n\n"
                "Touch them up in GIMP, then use \"Build PDF from PNG folder…\" "
                "to assemble the final print PDF from that folder."
            )

        worker.signals.finished.connect(done)
        worker.signals.error.connect(lambda msg: (progress.close(), QMessageBox.critical(self, "Export failed", msg)))
        self._run_worker(worker)

    def build_pdf_from_folder_action(self):
        folder = QFileDialog.getExistingDirectory(self, "Folder containing the (touched-up) PNG pages")
        if not folder:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save assembled PDF", "final_book.pdf", "PDF files (*.pdf)")
        if not path:
            return
        dpi = self.project.settings["dpi"]

        def task():
            return Project.build_pdf_from_folder(folder, path, dpi)

        worker = Worker(task)
        worker.signals.finished.connect(lambda p: QMessageBox.information(self, "Done", f"Assembled PDF saved to:\n{p}"))
        worker.signals.error.connect(lambda msg: QMessageBox.critical(self, "Failed", msg))
        self._run_worker(worker)

    # ==================================================================
    # project save / open
    # ==================================================================
    def save_project(self):
        if not self.project.pages:
            QMessageBox.information(self, "Nothing to save", "Add some pages first.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save project", "project.bookprep.json", "BookPrep project (*.json)")
        if not path:
            return
        data_dir = path + "_data"
        raw_subdir = os.path.join(data_dir, "raw")
        os.makedirs(raw_subdir, exist_ok=True)
        for page in self.project.pages:
            src = self.project.raw_path(page.uid)
            dst = os.path.join(raw_subdir, f"{page.uid}.png")
            if not os.path.exists(dst):
                shutil.copyfile(src, dst)
        retouch_pages = [p for p in self.project.pages if p.has_retouch]
        if retouch_pages:
            retouch_subdir = os.path.join(data_dir, "retouch")
            os.makedirs(retouch_subdir, exist_ok=True)
            for page in retouch_pages:
                src = self.project.retouch_path(page.uid)
                dst = os.path.join(retouch_subdir, f"{page.uid}.png")
                if os.path.exists(src) and not os.path.exists(dst):
                    shutil.copyfile(src, dst)
                # Content-rect sidecar (records where the content sat on
                # the canvas when the mask was saved -- see
                # Project.retouch_rect_path()) needs to travel with the
                # mask, or a reopened project silently falls back to
                # applying touch-ups unshifted the next time margins
                # change, same as a mask with no sidecar at all.
                rect_src = self.project.retouch_rect_path(page.uid)
                rect_dst = os.path.join(retouch_subdir, f"{page.uid}.rect.json")
                if os.path.exists(rect_src) and not os.path.exists(rect_dst):
                    shutil.copyfile(rect_src, rect_dst)
        self.project.save(path)
        QMessageBox.information(self, "Saved", f"Project saved to:\n{path}\n(page images in {data_dir})")

    def open_project(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open project", "", "BookPrep project (*.json)")
        if not path:
            return
        data_dir = path + "_data"
        if not os.path.isdir(os.path.join(data_dir, "raw")):
            QMessageBox.critical(self, "Cannot open",
                                  f"Expected page images alongside the project file at:\n{data_dir}")
            return
        old_project = self.project
        self.project = Project(work_dir=data_dir)
        self.project.load(path)
        self.project.backfill_legacy_retouch_rects()
        if old_project.work_dir.startswith(tempfile.gettempdir()):
            old_project.cleanup()
        self.thumb_cache = {}
        self.current_index = None
        self.apply_settings_to_form(self.project.settings)
        self.refresh_page_list()
        if self.project.pages:
            self.select_page(0)

    def apply_settings_to_form(self, s):
        """Push a loaded settings dict into every form widget.

        Every one of these widgets is wired so that changing its value
        fires on_settings_changed(), which reads ALL current widget
        values and overwrites self.project.settings (the very same dict
        `s` is) in one go. Since this method sets widgets one at a time,
        an unblocked signal firing partway through would read the
        widgets not yet updated (still showing their old/default value)
        and write those stale values back into `s` -- silently
        clobbering fields this same method hasn't gotten to yet. That
        was a real bug: loaded settings whose widget happens to come
        later in the list below (e.g. threshold_mode, margins) could get
        reverted to defaults by an earlier widget's setValue() call,
        while settings applied earlier in the sequence (e.g. dpi)
        happened to survive -- exactly the "threshold mode doesn't
        survive save/reload" symptom this was reported as.

        Fix: block every widget's signals for the whole sync, so nothing
        fires until all of them already show the correct loaded values,
        then manually re-run just the UI-only visibility side effect
        (which widgets should be shown for the loaded threshold mode)
        that on_threshold_mode_changed() would otherwise have triggered.
        """
        widgets = [
            self.trim_combo, self.custom_trim_w_spin, self.custom_trim_h_spin,
            self.dpi_spin, self.margin_top, self.margin_bottom,
            self.margin_inner, self.margin_outer, self.mirror_check, self.align_combo,
            self.contrast_slider, self.threshold_combo, self.fixed_thresh_slider,
            self.window_divisor_slider, self.binarize_k_slider, self.binarize_delta_slider,
            self.binarize_bounds_slider, self.savgol_check, self.morph_smoothing_check,
            self.thicken_slider, self.despeckle_combo, self.detect_minarea_slider,
            self.detect_gap_slider, self.detect_columngap_slider, self.detect_columnoverlap_slider,
            self.detect_pad_slider, self.detect_threshold_combo, self.detect_border_slider,
        ]
        for w in widgets:
            w.blockSignals(True)
        try:
            self.trim_combo.setCurrentIndex(self.trim_combo.findData(s["trim_preset"]))
            if s["trim_preset"] == "custom":
                self.custom_trim_w_spin.setValue(s["trim_w_in"])
                self.custom_trim_h_spin.setValue(s["trim_h_in"])
            self.dpi_spin.setValue(s["dpi"])
            self.margin_top.setValue(s["margin_top_in"])
            self.margin_bottom.setValue(s["margin_bottom_in"])
            self.margin_inner.setValue(s["margin_inner_in"])
            self.margin_outer.setValue(s["margin_outer_in"])
            self.mirror_check.setChecked(s["mirror_margins"])
            self.align_combo.setCurrentIndex(self.align_combo.findData(s["align"]))
            self.contrast_slider.setValue(int(round(s["contrast"] * 100)))
            self.threshold_combo.setCurrentIndex(self.threshold_combo.findData(s["threshold_mode"]))
            self.fixed_thresh_slider.setValue(s["fixed_threshold"])
            self.window_divisor_slider.setValue(s["window_divisor"])
            self.binarize_k_slider.setValue(int(round(s["binarize_k"] * 100)))
            self.binarize_delta_slider.setValue(int(round(s["binarize_delta"])))
            self.binarize_bounds_slider.setValue(s["binarize_lower"])
            self.savgol_check.setChecked(s["savgol_enabled"])
            self.morph_smoothing_check.setChecked(s["morph_smoothing"])
            self.thicken_slider.setValue(s["thicken_px"])
            self.despeckle_combo.setCurrentIndex(self.despeckle_combo.findData(s.get("despeckle_level", "normal")))
            self.detect_minarea_slider.setValue(round(s["detect_min_area_frac"] * 100000))
            self.detect_gap_slider.setValue(round(s["detect_cluster_gap_frac"] * 1000))
            self.detect_columngap_slider.setValue(round(s["detect_column_vgap_frac"] * 1000))
            self.detect_columnoverlap_slider.setValue(round(s["detect_column_overlap_frac"] * 100))
            self.detect_pad_slider.setValue(round(s["detect_pad_frac"] * 1000))
            self.detect_threshold_combo.setCurrentIndex(
                self.detect_threshold_combo.findData(s.get("detect_threshold_method", "otsu")))
            self.detect_border_slider.setValue(round(s.get("detect_border_strip_frac", 0.03) * 1000))
        finally:
            for w in widgets:
                w.blockSignals(False)

        # Signals were blocked throughout, so re-apply just the UI-only
        # visibility side effect (which rows are shown for this threshold
        # mode) that on_threshold_mode_changed() would normally trigger --
        # without also re-deriving project.settings from the form, which
        # is unnecessary here since `s` already *is* project.settings.
        self._update_threshold_mode_visibility()
        self._update_trim_size_visibility()

        if s.get("ref_text_width_in"):
            self.ref_readout.setText(f"reference text width: {s['ref_text_width_in']:.3f} in")
        else:
            self.ref_readout.setText("not set")

    # ==================================================================
    def closeEvent(self, event):
        from PySide6.QtCore import QThreadPool
        QThreadPool.globalInstance().waitForDone(3000)
        import pdf_render_worker
        pdf_render_worker.shutdown_executor()
        if self.project.work_dir.startswith(tempfile.gettempdir()):
            self.project.cleanup()
        event.accept()
