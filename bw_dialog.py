from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QComboBox, QCheckBox,
    QPushButton, QSlider, QLabel, QWidget, QFrame
)
from PySide6.QtCore import Qt


def build_slider(lo, hi, value, fmt, div):
    row = QWidget()
    h = QHBoxLayout(row)
    h.setContentsMargins(0, 0, 0, 0)
    slider = QSlider(Qt.Horizontal)
    slider.setRange(lo, hi)
    slider.setValue(value)
    label = QLabel(fmt.format(value / div))
    slider.valueChanged.connect(lambda v: label.setText(fmt.format(v / div)))
    h.addWidget(slider, stretch=1)
    h.addWidget(label)
    return slider, row


class ComboBox(QComboBox):
    """QComboBox that explicitly closes its own dropdown after a
    selection. Stock QComboBox normally does this on its own via a
    focus-out/grab-release event on the popup -- but some window
    managers (WSLg in particular) don't reliably deliver that event,
    leaving the dropdown visibly open until the user clicks elsewhere
    to dismiss it manually. Explicitly hiding it ourselves on
    `activated` (fires on a real user selection, not a programmatic
    .setCurrentIndex() call) sidesteps that regardless of the
    underlying cause, and is a no-op anywhere the popup already closes
    itself correctly."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.activated.connect(self.hidePopup)


class BWOverrideDialog(QDialog):
    """Set (or clear) a per-page black & white processing override for a
    batch of selected pages — or disable B&W processing entirely for
    them, keeping the page exactly as scanned (grayscale or color).
    Values are seeded from the first selected page's existing override if
    it has one, otherwise from the project's global settings."""

    def __init__(self, parent, initial: dict, page_count: int, initial_disabled: bool = False):
        super().__init__(parent)
        self.setWindowTitle(f"Black & white processing — {page_count} page(s)")
        self.setMinimumWidth(380)
        self.cleared = False

        layout = QVBoxLayout(self)
        note = QLabel(f"These settings will apply only to the {page_count} selected "
                       f"page(s), overriding the project's global black & white settings.")
        note.setWordWrap(True)
        layout.addWidget(note)

        self.disable_check = QCheckBox("Turn off B&W processing for these pages "
                                        "(keep the scan exactly as-is)")
        self.disable_check.setChecked(initial_disabled)
        self.disable_check.stateChanged.connect(self._toggle_enabled)
        layout.addWidget(self.disable_check)
        hint = QLabel("Cropping, resizing to match the rest of the book, and margins "
                       "still apply — only contrast/threshold/thicken/despeckle are skipped.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #666; font-size: 11px;")
        layout.addWidget(hint)

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        layout.addWidget(sep)

        self._controls_widget = QWidget()
        form = QFormLayout(self._controls_widget)

        preset_btn = QPushButton("Preset: flatten to monochrome (no enhancements)")
        preset_btn.setToolTip(
            "For a page that's really just black-and-white text but is\n"
            "carrying incidental grayscale (e.g. after cropping or painting\n"
            "out a watermark) -- sets Otsu global auto-threshold with\n"
            "contrast/thicken/despeckle/smoothing all off, so nothing\n"
            "changes except collapsing to pure black and white.")
        preset_btn.clicked.connect(self._apply_flatten_preset)
        form.addRow(preset_btn)

        self.contrast_slider, contrast_row = build_slider(
            100, 250, int(round(initial["contrast"] * 100)), "{:.2f}", 100)
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
        idx = self.threshold_combo.findData(initial["threshold_mode"])
        self.threshold_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.threshold_combo.currentIndexChanged.connect(self._toggle_fixed_row)
        form.addRow("Threshold mode", self.threshold_combo)

        self.fixed_slider, self.fixed_row = build_slider(
            100, 240, initial["fixed_threshold"], "{:.0f}", 1)
        form.addRow("Fixed level", self.fixed_row)

        self.window_divisor_slider, window_divisor_row = build_slider(
            6, 40, initial.get("window_divisor", 12), "{:.0f}", 1)
        self.window_divisor_label = QLabel("Window size (÷ page dim.)")
        form.addRow(self.window_divisor_label, window_divisor_row)

        self.binarize_k_slider, binarize_k_row = build_slider(
            0, 150, int(round(initial.get("binarize_k", 0.34) * 100)), "{:.2f}", 100)
        self.binarize_k_label = QLabel("Sensitivity (k)")
        form.addRow(self.binarize_k_label, binarize_k_row)

        self.binarize_delta_slider, binarize_delta_row = build_slider(
            -30, 30, int(round(initial.get("binarize_delta", 0.0))), "{:.0f}", 1)
        self.binarize_delta_label = QLabel("Threshold shift")
        form.addRow(self.binarize_delta_label, binarize_delta_row)

        self.binarize_bounds_slider, binarize_bounds_row = build_slider(
            0, 254, initial.get("binarize_lower", 1), "{:.0f}", 1)
        self.binarize_bounds_label = QLabel("Lower bound (pure-black cutoff)")
        form.addRow(self.binarize_bounds_label, binarize_bounds_row)

        self.savgol_check = QCheckBox("Savitzky-Golay smoothing before thresholding")
        self.savgol_check.setChecked(initial.get("savgol_enabled", False))
        form.addRow(self.savgol_check)

        self.morph_smoothing_check = QCheckBox("Morphological smoothing after thresholding")
        self.morph_smoothing_check.setChecked(initial.get("morph_smoothing", False))
        form.addRow(self.morph_smoothing_check)

        self._advanced_rows = [
            (self.window_divisor_label, window_divisor_row),
            (self.binarize_k_label, binarize_k_row),
            (self.binarize_delta_label, binarize_delta_row),
            (self.savgol_check, None),
            (self.morph_smoothing_check, None),
        ]
        self._bounds_rows = [(self.binarize_bounds_label, binarize_bounds_row)]

        self.thicken_slider, thicken_row = build_slider(
            0, 4, initial["thicken_px"], "{:.0f}px", 1)
        form.addRow("Thicken strokes", thicken_row)

        self.despeckle_combo = ComboBox()
        self.despeckle_combo.addItem("Off", "off")
        self.despeckle_combo.addItem("Cautious", "cautious")
        self.despeckle_combo.addItem("Normal (recommended)", "normal")
        self.despeckle_combo.addItem("Aggressive", "aggressive")
        idx = self.despeckle_combo.findData(initial.get("despeckle_level", "normal"))
        self.despeckle_combo.setCurrentIndex(idx if idx >= 0 else 0)
        form.addRow("Remove scan speckle", self.despeckle_combo)
        layout.addWidget(self._controls_widget)
        self._toggle_fixed_row()
        self._toggle_enabled()

        btn_row = QHBoxLayout()
        clear_btn = QPushButton("Clear override (use global settings)")
        clear_btn.clicked.connect(self._on_clear)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        apply_btn = QPushButton(f"Apply to {page_count} page(s)")
        apply_btn.setDefault(True)
        apply_btn.clicked.connect(self.accept)
        btn_row.addWidget(clear_btn)
        btn_row.addStretch()
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(apply_btn)
        layout.addLayout(btn_row)

    def _toggle_fixed_row(self):
        mode = self.threshold_combo.currentData()
        is_fixed = mode == "fixed"
        self.fixed_row.setVisible(is_fixed)

        is_advanced = mode not in ("adaptive", "fixed", "otsu")
        for label, row in self._advanced_rows:
            label.setVisible(is_advanced)
            if row is not None:
                row.setVisible(is_advanced)

        needs_bounds = mode in ("wolf", "fox", "window", "grad")
        for label, row in self._bounds_rows:
            label.setVisible(is_advanced and needs_bounds)
            row.setVisible(is_advanced and needs_bounds)

    def _toggle_enabled(self):
        self._controls_widget.setEnabled(not self.disable_check.isChecked())

    def _apply_flatten_preset(self):
        # Make sure B&W processing is actually on -- the preset works by
        # running a neutral threshold pass, so it has no effect at all if
        # "keep the scan exactly as-is" is checked (the controls are
        # disabled in that case, matching the checkbox's own behavior).
        self.disable_check.setChecked(False)
        self.contrast_slider.setValue(100)  # 1.00 -- no contrast adjustment
        idx = self.threshold_combo.findData("otsu")
        self.threshold_combo.setCurrentIndex(idx)
        self.thicken_slider.setValue(0)
        despeckle_idx = self.despeckle_combo.findData("off")
        self.despeckle_combo.setCurrentIndex(despeckle_idx)
        self.savgol_check.setChecked(False)
        self.morph_smoothing_check.setChecked(False)

    def _on_clear(self):
        self.cleared = True
        self.accept()

    @property
    def disabled(self):
        return self.disable_check.isChecked()

    def result_override(self):
        """None means 'clear the override', otherwise a settings dict.
        Meaningless (and not applied) when .disabled is True."""
        if self.cleared:
            return None
        return {
            "contrast": self.contrast_slider.value() / 100.0,
            "threshold_mode": self.threshold_combo.currentData(),
            "fixed_threshold": self.fixed_slider.value(),
            "window_divisor": self.window_divisor_slider.value(),
            "binarize_k": self.binarize_k_slider.value() / 100.0,
            "binarize_delta": float(self.binarize_delta_slider.value()),
            "binarize_lower": self.binarize_bounds_slider.value(),
            "savgol_enabled": self.savgol_check.isChecked(),
            "morph_smoothing": self.morph_smoothing_check.isChecked(),
            "thicken_px": self.thicken_slider.value(),
            "despeckle_level": self.despeckle_combo.currentData(),
        }
