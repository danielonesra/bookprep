from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QRadioButton, QButtonGroup,
    QDoubleSpinBox, QPushButton, QLabel, QWidget
)
from PySide6.QtCore import Qt


class SizeOverrideDialog(QDialog):
    """Set (or clear) a per-page size override for a batch of selected
    pages: either a relative percentage of the automatic size, or an
    absolute target width in inches. Only one is active at a time,
    chosen via the radio buttons; the inactive field is disabled but
    still visible so switching back and forth doesn't lose your place."""

    def __init__(self, parent, initial: dict, page_count: int, default_absolute_in: float = 4.0):
        super().__init__(parent)
        self.setWindowTitle(f"Page size — {page_count} page(s)")
        self.setMinimumWidth(360)
        self.cleared = False

        layout = QVBoxLayout(self)
        note = QLabel(f"Adjust the size of the {page_count} selected page(s) relative to "
                       f"(or instead of) the automatic sizing.")
        note.setWordWrap(True)
        layout.addWidget(note)

        self.button_group = QButtonGroup(self)
        self.relative_radio = QRadioButton("Relative — percentage of the automatic size")
        self.absolute_radio = QRadioButton("Absolute — exact width")
        self.button_group.addButton(self.relative_radio)
        self.button_group.addButton(self.absolute_radio)
        layout.addWidget(self.relative_radio)

        form = QFormLayout()
        self.relative_spin = QDoubleSpinBox()
        self.relative_spin.setRange(10.0, 400.0)
        self.relative_spin.setDecimals(0)
        self.relative_spin.setSingleStep(5.0)
        self.relative_spin.setSuffix("%")
        form.addRow("Scale", self.relative_spin)
        layout.addLayout(form)

        layout.addWidget(self.absolute_radio)
        form2 = QFormLayout()
        self.absolute_spin = QDoubleSpinBox()
        self.absolute_spin.setRange(0.25, 20.0)
        self.absolute_spin.setDecimals(3)
        self.absolute_spin.setSingleStep(0.05)
        self.absolute_spin.setSuffix(" in")
        form2.addRow("Width", self.absolute_spin)
        layout.addLayout(form2)

        hint = QLabel("Either way, a page is only ever shrunk to fit the margins, never "
                       "grown past them.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #666; font-size: 11px;")
        layout.addWidget(hint)

        # seed initial state
        mode = (initial or {}).get("mode", "relative")
        value = (initial or {}).get("value")
        if mode == "absolute":
            self.absolute_radio.setChecked(True)
            self.absolute_spin.setValue(value if value else default_absolute_in)
            self.relative_spin.setValue(100.0)
        else:
            self.relative_radio.setChecked(True)
            self.relative_spin.setValue((value * 100.0) if value else 100.0)
            self.absolute_spin.setValue(default_absolute_in)

        self.relative_radio.toggled.connect(self._toggle_enabled)
        self._toggle_enabled()

        btn_row = QHBoxLayout()
        clear_btn = QPushButton("Clear override (use automatic sizing)")
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

    def _toggle_enabled(self):
        is_relative = self.relative_radio.isChecked()
        self.relative_spin.setEnabled(is_relative)
        self.absolute_spin.setEnabled(not is_relative)

    def _on_clear(self):
        self.cleared = True
        self.accept()

    def result_override(self):
        """None means 'clear the override', otherwise a size_override dict."""
        if self.cleared:
            return None
        if self.absolute_radio.isChecked():
            return {"mode": "absolute", "value": self.absolute_spin.value()}
        return {"mode": "relative", "value": self.relative_spin.value() / 100.0}
