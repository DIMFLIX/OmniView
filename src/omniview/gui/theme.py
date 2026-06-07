"""Centralized stylesheet and color constants for OmniView Dashboard.

All visual theming lives here so that individual widgets stay clean
and the entire look can be tweaked from a single place.
"""

from __future__ import annotations

from PyQt6.QtGui import QColor, QPalette


# ---------------------------------------------------------------------------
# Color palette
# ---------------------------------------------------------------------------

class C:
    """Named color constants used throughout the app."""

    BG_DARK      = "#0d1117"
    BG_PANEL     = "#161b22"
    BG_INPUT     = "#0d1117"
    BG_CARD      = "#1c2333"
    BORDER       = "#30363d"
    BORDER_LIGHT = "#484f58"
    ACCENT       = "#58a6ff"
    ACCENT_DIM   = "#1f6feb"
    GREEN        = "#3fb950"
    ORANGE       = "#d29922"
    RED          = "#f85149"
    TEXT         = "#e6edf3"
    TEXT_DIM     = "#8b949e"
    TEXT_MUTED   = "#484f58"
    OVERLAY_BG   = "rgba(13, 17, 23, 200)"


# ---------------------------------------------------------------------------
# Master application stylesheet
# ---------------------------------------------------------------------------

# Path to bundled SVG icons (resolved at import time)
from pathlib import Path as _P

_ICONS = _P(__file__).parent / "icons"
_CHECK  = str(_ICONS / "checkmark.svg")
_UP     = str(_ICONS / "arrow-up.svg")
_DOWN   = str(_ICONS / "arrow-down.svg")

APP_STYLESHEET = f"""
/* ---- Global ---- */
QWidget {{
    font-family: "Segoe UI", "Inter", "Noto Sans", sans-serif;
    font-size: 13px;
    color: {C.TEXT};
}}

QMainWindow {{
    background: {C.BG_DARK};
}}

/* ---- Splitter ---- */
QSplitter::handle {{
    background: {C.BORDER};
    width: 2px;
}}

/* ---- Scroll area ---- */
QScrollArea {{
    background: {C.BG_DARK};
    border: none;
}}
QScrollBar:vertical {{
    background: {C.BG_DARK};
    width: 8px;
    border-radius: 4px;
}}
QScrollBar::handle:vertical {{
    background: {C.BORDER_LIGHT};
    min-height: 30px;
    border-radius: 4px;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}

/* ---- Group boxes ---- */
QGroupBox {{
    font-weight: 600;
    font-size: 12px;
    color: {C.ACCENT};
    border: 1px solid {C.BORDER};
    border-radius: 8px;
    margin-top: 12px;
    padding: 16px 10px 10px 10px;
    background: {C.BG_PANEL};
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 14px;
    padding: 0 6px;
}}

/* ---- Icon group box body ---- */
QWidget#iconGroupBoxBody {{
    border: 1px solid {C.BORDER};
    border-radius: 8px;
    background: {C.BG_PANEL};
    padding: 10px;
}}

/* ---- Spin boxes ---- */
QSpinBox, QDoubleSpinBox {{
    background: {C.BG_INPUT};
    border: 1px solid {C.BORDER};
    border-radius: 6px;
    padding: 4px 8px;
    color: {C.TEXT};
    min-height: 28px;
    selection-background-color: {C.ACCENT_DIM};
}}
QSpinBox:focus, QDoubleSpinBox:focus {{
    border-color: {C.ACCENT};
}}
QSpinBox::up-button, QDoubleSpinBox::up-button,
QSpinBox::down-button, QDoubleSpinBox::down-button {{
    width: 20px;
    border: none;
    background: transparent;
}}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{
    image: url({_UP});
    width: 12px;
    height: 12px;
}}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{
    image: url({_DOWN});
    width: 12px;
    height: 12px;
}}
QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover {{
    background: {C.BORDER};
}}
QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {{
    background: {C.BORDER};
}}

/* ---- Combo box ---- */
QComboBox {{
    background: {C.BG_INPUT};
    border: 1px solid {C.BORDER};
    border-radius: 6px;
    padding: 4px 10px;
    color: {C.TEXT};
    min-height: 28px;
}}
QComboBox:focus {{
    border-color: {C.ACCENT};
}}
QComboBox::drop-down {{
    border: none;
    width: 24px;
}}
QComboBox::down-arrow {{
    image: url({_DOWN});
    width: 12px;
    height: 12px;
}}
QComboBox QAbstractItemView {{
    background: {C.BG_PANEL};
    border: 1px solid {C.BORDER};
    border-radius: 6px;
    color: {C.TEXT};
    selection-background-color: {C.ACCENT_DIM};
    outline: none;
}}

/* ---- Check box ---- */
QCheckBox {{
    spacing: 8px;
    color: {C.TEXT};
}}
QCheckBox::indicator {{
    width: 18px;
    height: 18px;
    border-radius: 4px;
    border: 2px solid {C.BORDER_LIGHT};
    background: {C.BG_INPUT};
}}
QCheckBox::indicator:checked {{
    background: {C.ACCENT};
    border-color: {C.ACCENT};
    image: url({_CHECK});
}}
QCheckBox::indicator:hover {{
    border-color: {C.ACCENT};
}}

/* ---- Plain text edit (log viewer) ---- */
QPlainTextEdit {{
    background: {C.BG_INPUT};
    border: 1px solid {C.BORDER};
    border-radius: 6px;
    color: {C.TEXT_DIM};
    font-family: "JetBrains Mono", "Fira Code", "Cascadia Code", monospace;
    font-size: 11px;
    padding: 6px;
    selection-background-color: {C.ACCENT_DIM};
}}

/* ---- Labels ---- */
QLabel {{
    color: {C.TEXT};
    background: transparent;
}}

/* ---- Tooltips ---- */
QToolTip {{
    background: {C.BG_PANEL};
    color: {C.TEXT};
    border: 1px solid {C.BORDER};
    border-radius: 4px;
    padding: 4px;
}}
"""


# ---------------------------------------------------------------------------
# Camera widget stylesheet
# ---------------------------------------------------------------------------

CAMERA_WIDGET_STYLE = f"""
QLabel {{
    background: {C.BG_CARD};
    border: 2px solid {C.BORDER};
    border-radius: 10px;
}}
"""

CAMERA_WIDGET_FULLSCREEN_STYLE = f"""
QLabel {{
    background: {C.BG_CARD};
    border: 3px solid {C.ACCENT};
    border-radius: 10px;
}}
"""

CAMERA_WIDGET_HOVER_STYLE = f"""
QLabel {{
    background: {C.BG_CARD};
    border: 2px solid {C.BORDER_LIGHT};
    border-radius: 10px;
}}
"""


# ---------------------------------------------------------------------------
# No-camera placeholder stylesheet
# ---------------------------------------------------------------------------

NO_CAMERA_STYLE = f"""
QLabel {{
    color: {C.TEXT_DIM};
    font-size: 24px;
    font-weight: 600;
    background: transparent;
}}
"""


# ---------------------------------------------------------------------------
# Overlay stylesheet
# ---------------------------------------------------------------------------

OVERLAY_STYLE = f"""
QLabel {{
    background: {C.OVERLAY_BG};
    color: {C.ACCENT};
    font-size: 18px;
    font-weight: 600;
    border-radius: 12px;
    padding: 24px;
}}
"""


# ---------------------------------------------------------------------------
# Dark palette factory
# ---------------------------------------------------------------------------

def dark_palette() -> QPalette:
    """Build a QPalette that matches our stylesheet colors."""
    p = QPalette()
    p.setColor(QPalette.ColorRole.Window,          QColor(C.BG_DARK))
    p.setColor(QPalette.ColorRole.WindowText,      QColor(C.TEXT))
    p.setColor(QPalette.ColorRole.Base,            QColor(C.BG_INPUT))
    p.setColor(QPalette.ColorRole.AlternateBase,   QColor(C.BG_PANEL))
    p.setColor(QPalette.ColorRole.ToolTipBase,     QColor(C.BG_PANEL))
    p.setColor(QPalette.ColorRole.ToolTipText,     QColor(C.TEXT))
    p.setColor(QPalette.ColorRole.Text,            QColor(C.TEXT))
    p.setColor(QPalette.ColorRole.Button,          QColor(C.BG_PANEL))
    p.setColor(QPalette.ColorRole.ButtonText,      QColor(C.TEXT))
    p.setColor(QPalette.ColorRole.BrightText,      QColor(C.RED))
    p.setColor(QPalette.ColorRole.Highlight,       QColor(C.ACCENT_DIM))
    p.setColor(QPalette.ColorRole.HighlightedText, QColor(C.TEXT))
    p.setColor(QPalette.ColorRole.PlaceholderText, QColor(C.TEXT_MUTED))
    return p
