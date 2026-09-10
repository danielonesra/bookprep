from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QRadioButton, QButtonGroup,
    QSpinBox, QPushButton, QLabel
)


class MovePagesDialog(QDialog):
    """Move a batch of selected pages to before or after a specific
    (existing) page, referred to by its current page number.

    `selected_rows` (0-based) is used only for validation/labeling —
    picking a target page number that falls inside the current
    selection is ambiguous (which of the selected pages would the
    anchor even be, once they're the ones moving?), so that's disabled
    rather than guessed at.
    """

    def __init__(self, parent, page_count: int, selected_rows: list):
        super().__init__(parent)
        n = len(selected_rows)
        self.setWindowTitle(f"Move {n} page(s)…" if n != 1 else "Move page…")
        self.setMinimumWidth(320)
        self._page_count = page_count
        self._selected_rows = set(selected_rows)

        layout = QVBoxLayout(self)
        note = QLabel(f"Move the {n} selected page(s) to a new position, "
                       f"relative to another page.")
        note.setWordWrap(True)
        layout.addWidget(note)

        radio_row = QHBoxLayout()
        self.button_group = QButtonGroup(self)
        self.before_radio = QRadioButton("Before page")
        self.after_radio = QRadioButton("After page")
        self.button_group.addButton(self.before_radio)
        self.button_group.addButton(self.after_radio)
        self.after_radio.setChecked(True)
        radio_row.addWidget(self.before_radio)
        radio_row.addWidget(self.after_radio)
        layout.addLayout(radio_row)

        form = QFormLayout()
        self.page_spin = QSpinBox()
        self.page_spin.setRange(1, page_count)
        # Default to just after the last selected page -- a no-op-ish
        # starting point rather than an arbitrary one.
        default_row = max(selected_rows) if selected_rows else 0
        self.page_spin.setValue(min(default_row + 1, page_count))
        form.addRow("Page number", self.page_spin)
        layout.addLayout(form)

        self.warning_label = QLabel()
        self.warning_label.setWordWrap(True)
        self.warning_label.setStyleSheet("color: #b00020; font-size: 11px;")
        layout.addWidget(self.warning_label)

        self.page_spin.valueChanged.connect(self._validate)

        btn_row = QHBoxLayout()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        self.move_btn = QPushButton(f"Move {n} page(s)" if n != 1 else "Move page")
        self.move_btn.setDefault(True)
        self.move_btn.clicked.connect(self.accept)
        btn_row.addStretch()
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(self.move_btn)
        layout.addLayout(btn_row)

        self._validate()

    def _validate(self):
        target_row = self.page_spin.value() - 1  # 0-based
        if target_row in self._selected_rows:
            self.warning_label.setText(
                "That page is part of the current selection — pick a page outside it.")
            self.move_btn.setEnabled(False)
        else:
            self.warning_label.setText("")
            self.move_btn.setEnabled(True)

    def anchor_row_and_position(self):
        """Returns (anchor_row, position) where anchor_row is the 0-based
        index (in the *current*, pre-move list) of the reference page,
        and position is "before" or "after"."""
        anchor_row = self.page_spin.value() - 1
        position = "before" if self.before_radio.isChecked() else "after"
        return anchor_row, position
