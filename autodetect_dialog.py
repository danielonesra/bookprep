from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QRadioButton, QButtonGroup,
    QCheckBox, QPushButton, QLabel
)


class AutoDetectDialog(QDialog):
    """Ask which pages to run auto-detection on, and which operations
    to run: auto-crop, auto-deskew, or both. Checking auto-deskew alone
    already gets a full crop -> deskew -> re-crop pass per page (see
    Project.run_autodetect()); auto-crop here means an additional,
    independent fresh crop pass on its own."""

    def __init__(self, parent, page_count: int, selected_count: int):
        super().__init__(parent)
        self.setWindowTitle("Auto-detect…")
        self.setMinimumWidth(340)

        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("Pages:"))
        range_row = QHBoxLayout()
        self.button_group = QButtonGroup(self)
        self.all_radio = QRadioButton(f"All pages ({page_count})")
        self.selected_radio = QRadioButton(
            f"Selected pages ({selected_count})" if selected_count else "Selected pages")
        self.selected_radio.setEnabled(selected_count > 0)
        self.button_group.addButton(self.all_radio)
        self.button_group.addButton(self.selected_radio)
        if selected_count > 0:
            self.selected_radio.setChecked(True)
        else:
            self.all_radio.setChecked(True)
        range_row.addWidget(self.all_radio)
        range_row.addWidget(self.selected_radio)
        layout.addLayout(range_row)

        layout.addWidget(QLabel("Run:"))
        self.crop_check = QCheckBox("Auto-crop")
        self.crop_check.setChecked(True)
        self.deskew_check = QCheckBox("Auto-deskew")
        self.deskew_check.setToolTip(
            "Also straightens each page. This already includes its own "
            "crop pass before and after deskewing (crop \u2192 deskew \u2192 "
            "re-crop tighter), whether or not Auto-crop above is checked.")
        layout.addWidget(self.crop_check)
        layout.addWidget(self.deskew_check)

        self.warning_label = QLabel("Select at least one operation to run.")
        self.warning_label.setWordWrap(True)
        self.warning_label.setStyleSheet("color: #b00020; font-size: 11px;")
        layout.addWidget(self.warning_label)
        self.warning_label.setVisible(False)

        self.crop_check.toggled.connect(self._validate)
        self.deskew_check.toggled.connect(self._validate)

        btn_row = QHBoxLayout()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        self.run_btn = QPushButton("Run")
        self.run_btn.setDefault(True)
        self.run_btn.clicked.connect(self.accept)
        btn_row.addStretch()
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(self.run_btn)
        layout.addLayout(btn_row)

        self._validate()

    def _validate(self):
        ok = self.crop_check.isChecked() or self.deskew_check.isChecked()
        self.warning_label.setVisible(not ok)
        self.run_btn.setEnabled(ok)

    def selection(self):
        """Returns (use_selected: bool, do_crop: bool, do_deskew: bool)."""
        return (self.selected_radio.isChecked(),
                self.crop_check.isChecked(),
                self.deskew_check.isChecked())
