#!/usr/bin/env python3
"""Entry point for the OmniView Dashboard application.

Usage:
    omniview-dashboard
        (after ``pip install omniview[gui]``)

Or:
    python -m omniview.gui.main
"""

from __future__ import annotations

import sys

try:
    from PyQt6.QtWidgets import QApplication
except ImportError:
    sys.exit(
        "PyQt6 is required for the GUI. "
        "Install it with: pip install omniview[gui]"
    )

from .dashboard import Dashboard
from .theme import APP_STYLESHEET, dark_palette


def main() -> int:
    """Create the QApplication and show the Dashboard."""
    app = QApplication(sys.argv)

    # Fully custom dark theme — no system styles
    app.setStyle("Fusion")  # Fusion as the base (clean geometric shapes)
    app.setPalette(dark_palette())
    app.setStyleSheet(APP_STYLESHEET)

    window = Dashboard()
    window.show()

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
