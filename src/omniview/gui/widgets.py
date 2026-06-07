"""Custom widgets for the OmniView Dashboard."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .theme import C


class IconGroupBox(QWidget):
    """A QGroupBox replacement that shows an SVG icon next to the title.

    Uses a ``QLabel`` with a ``QIcon`` for the icon and a styled
    ``QLabel`` for the title.  The body area is a ``QWidget`` named
    ``iconGroupBoxBody`` so the stylesheet can target it (border,
    border-radius, background).

    Usage::

        grp = IconGroupBox("Параметры захвата", "gui/icons/gear.svg")
        layout = grp.body_layout()
        layout.addWidget(some_widget)
    """

    def __init__(
        self, title: str, icon_path: str, parent=None
    ) -> None:
        super().__init__(parent)
        self._title = title
        self._icon_path = icon_path

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # --- Title row (icon + label, horizontal) ---
        header = QWidget()
        header.setStyleSheet("background: transparent;")
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(14, 0, 10, 0)
        header_layout.setSpacing(0)

        # Icon + title in one horizontal row
        title_row = QWidget()
        title_row.setStyleSheet("background: transparent;")
        row_layout = QHBoxLayout(title_row)
        row_layout.setContentsMargins(0, 6, 0, 0)
        row_layout.setSpacing(6)
        row_layout.setAlignment(Qt.AlignmentFlag.AlignLeft)

        icon_label = QLabel()
        icon_label.setFixedWidth(16)
        icon_label.setFixedHeight(16)
        icon_label.setStyleSheet("background: transparent;")
        if Path(icon_path).exists():
            icon_label.setPixmap(QIcon(icon_path).pixmap(16, 16))
        row_layout.addWidget(icon_label)

        title_label = QLabel(title)
        title_label.setStyleSheet(
            f"color: {C.ACCENT}; font-weight: 600; font-size: 12px;"
            f" background: transparent; border: none; padding: 0;"
        )
        row_layout.addWidget(title_label)

        header_layout.addWidget(title_row)
        outer.addWidget(header)

        # --- Body (the bordered content area) ---
        self._body = QWidget()
        self._body.setObjectName("iconGroupBoxBody")
        self._body_layout = QVBoxLayout(self._body)
        self._body_layout.setContentsMargins(10, 10, 10, 10)
        self._body_layout.setSpacing(6)
        outer.addWidget(self._body)

    def body_layout(self) -> QVBoxLayout:
        """Return the layout inside the bordered body area."""
        return self._body_layout
