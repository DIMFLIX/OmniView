"""SettingsPanel — the right-side (25%) configuration panel.

Contains:
- **Capture Parameters** group: Width / Height / FPS spin-boxes + HW accel.
- **IP Cameras / Video files** group: editable text area for RTSP/file URLs.
- **Orchestration** group: Sequential mode checkbox + switch interval.
- **Demo Filters** group: filter combo-box.
- **Log Viewer**: read-only ``QPlainTextEdit``.

All input widgets emit a debounced ``settings_changed`` signal so that
the Dashboard can restart the manager with new parameters.
"""

from __future__ import annotations

from pathlib import Path as _P

from PyQt6.QtCore import QTimer, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .filters import FILTER_NAMES
from .theme import C
from .widgets import IconGroupBox

_ICONS = _P(__file__).parent / "icons"


class SettingsPanel(QWidget):
    """Right-side panel with capture params, orchestration, filter, and logs."""

    settings_changed = pyqtSignal(dict)  # dict of attr-name -> value

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumWidth(220)
        self._build_ui()
        self._debounce_timer = QTimer(self)
        self._debounce_timer.setSingleShot(True)
        self._debounce_timer.setInterval(400)  # ms
        self._debounce_timer.timeout.connect(self._emit_settings)
        self._connect_signals()

    # -- public API ----------------------------------------------------------

    def append_log(self, text: str) -> None:
        """Append a line to the log viewer."""
        self._log_viewer.appendPlainText(text)
        scrollbar = self._log_viewer.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def current_settings(self) -> dict:
        """Return the current values of all settings as a dict."""
        # Determine multiplex mode from the combo box
        multiplex_mode = self._combo_multiplex.currentData()
        if not self._check_multiplex.isChecked():
            multiplex_mode = "off"

        return {
            "frame_width": self._spin_width.value(),
            "frame_height": self._spin_height.value(),
            "fps": self._spin_fps.value(),
            "hw_acceleration": self._check_hw_accel.isChecked(),
            "sequential_mode": self._check_sequential.isChecked(),
            "switch_interval": self._spin_interval.value(),
            "rtsp_urls": self._parse_rtsp_urls(),
            "multiplex_mode": multiplex_mode,
            "multiplex_slots": self._spin_multiplex_slots.value(),
            "multiplex_dwell": self._spin_multiplex_dwell.value(),
            "multiplex_settle": self._spin_multiplex_settle.value(),
            "multiplex_backend": self._combo_multiplex_backend.currentData(),
        }

    def _parse_rtsp_urls(self) -> list[str]:
        """Parse non-empty lines from the RTSP text area."""
        text = self._rtsp_edit.toPlainText().strip()
        if not text:
            return []
        return [line.strip() for line in text.splitlines() if line.strip()]

    def current_filter(self) -> str:
        """Return the name of the currently selected filter."""
        return self._combo_filter.currentText()

    # -- UI construction -----------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(12)

        # --- Capture Parameters ---
        grp_capture = IconGroupBox("Параметры захвата", str(_ICONS / "gear.svg"))
        cap_layout = grp_capture.body_layout()

        self._spin_width = self._make_spin(160, 3840, 640, 160, " px")
        self._spin_height = self._make_spin(120, 2160, 480, 120, " px")
        self._spin_fps = self._make_spin(1, 120, 30, 1, " fps")

        for label_text, spin in [
            ("Width", self._spin_width),
            ("Height", self._spin_height),
            ("FPS", self._spin_fps),
        ]:
            row = QHBoxLayout()
            lbl = QLabel(label_text)
            lbl.setFixedWidth(54)
            lbl.setStyleSheet(f"color: {C.TEXT_DIM}; font-size: 12px;")
            row.addWidget(lbl)
            row.addWidget(spin)
            cap_layout.addLayout(row)

        self._check_hw_accel = QCheckBox("HW Acceleration")
        self._check_hw_accel.setChecked(True)
        self._check_hw_accel.setToolTip(
            "GPU-accelerated decoding (VAAPI/D3D11). Falls back to software."
        )
        cap_layout.addWidget(self._check_hw_accel)

        layout.addWidget(grp_capture)

        # --- IP Cameras / Video Files ---
        grp_ip = IconGroupBox("IP камеры / видеофайлы", str(_ICONS / "satellite.svg"))
        ip_layout = grp_ip.body_layout()
        ip_layout.setSpacing(4)

        hint = QLabel("RTSP / файл — по одному на строку:")
        hint.setStyleSheet(f"color: {C.TEXT_DIM}; font-size: 11px;")
        ip_layout.addWidget(hint)

        self._rtsp_edit = QPlainTextEdit()
        self._rtsp_edit.setMaximumHeight(80)
        self._rtsp_edit.setPlaceholderText(
            "rtsp://admin:pass@192.168.1.10:554/stream\n"
            "/path/to/video.mp4"
        )
        ip_layout.addWidget(self._rtsp_edit)

        layout.addWidget(grp_ip)

        # --- Orchestration ---
        grp_orch = IconGroupBox("Режим оркестрации", str(_ICONS / "refresh.svg"))
        orch_layout = grp_orch.body_layout()

        self._check_sequential = QCheckBox("Sequential Mode")
        orch_layout.addWidget(self._check_sequential)

        row_interval = QHBoxLayout()
        lbl_interval = QLabel("Interval")
        lbl_interval.setFixedWidth(54)
        lbl_interval.setStyleSheet(f"color: {C.TEXT_DIM}; font-size: 12px;")
        self._spin_interval = QDoubleSpinBox()
        self._spin_interval.setRange(0.5, 60.0)
        self._spin_interval.setSingleStep(0.5)
        self._spin_interval.setValue(3.0)
        self._spin_interval.setSuffix(" sec")
        self._spin_interval.setEnabled(False)
        row_interval.addWidget(lbl_interval)
        row_interval.addWidget(self._spin_interval)
        orch_layout.addLayout(row_interval)

        layout.addWidget(grp_orch)

        # --- Multiplex (USB Hub Rotation) ---
        grp_mpx = IconGroupBox("Мультиплекс USB", str(_ICONS / "refresh.svg"))
        mpx_layout = grp_mpx.body_layout()

        self._check_multiplex = QCheckBox("Auto Multiplex")
        self._check_multiplex.setChecked(True)
        self._check_multiplex.setToolTip(
            "Автоматическое вращение камер на USB-хабе\n"
            "(K слотов для N камер, N > K)"
        )
        mpx_layout.addWidget(self._check_multiplex)

        # Mode combo: auto / force
        row_mode = QHBoxLayout()
        lbl_mode = QLabel("Режим")
        lbl_mode.setFixedWidth(54)
        lbl_mode.setStyleSheet(f"color: {C.TEXT_DIM}; font-size: 12px;")
        self._combo_multiplex = QComboBox()
        self._combo_multiplex.addItem("Авто (по топологии)", "auto")
        self._combo_multiplex.addItem("Принудительно", "force")
        row_mode.addWidget(lbl_mode)
        row_mode.addWidget(self._combo_multiplex)
        mpx_layout.addLayout(row_mode)

        # Slots
        row_slots = QHBoxLayout()
        lbl_slots = QLabel("Слоты")
        lbl_slots.setFixedWidth(54)
        lbl_slots.setStyleSheet(f"color: {C.TEXT_DIM}; font-size: 12px;")
        self._spin_multiplex_slots = self._make_spin(1, 8, 2, 1, "")
        self._spin_multiplex_slots.setToolTip("Макс. одновременных стримов на хаб (K)")
        row_slots.addWidget(lbl_slots)
        row_slots.addWidget(self._spin_multiplex_slots)
        mpx_layout.addLayout(row_slots)

        # Dwell
        row_dwell = QHBoxLayout()
        lbl_dwell = QLabel("Dwell")
        lbl_dwell.setFixedWidth(54)
        lbl_dwell.setStyleSheet(f"color: {C.TEXT_DIM}; font-size: 12px;")
        self._spin_multiplex_dwell = QDoubleSpinBox()
        self._spin_multiplex_dwell.setRange(0.3, 30.0)
        self._spin_multiplex_dwell.setSingleStep(0.3)
        self._spin_multiplex_dwell.setValue(1.5)
        self._spin_multiplex_dwell.setSuffix(" sec")
        self._spin_multiplex_dwell.setToolTip("Время жизни камеры в окне перед ротацией")
        row_dwell.addWidget(lbl_dwell)
        row_dwell.addWidget(self._spin_multiplex_dwell)
        mpx_layout.addLayout(row_dwell)

        # Settle
        row_settle = QHBoxLayout()
        lbl_settle = QLabel("Settle")
        lbl_settle.setFixedWidth(54)
        lbl_settle.setStyleSheet(f"color: {C.TEXT_DIM}; font-size: 12px;")
        self._spin_multiplex_settle = QDoubleSpinBox()
        self._spin_multiplex_settle.setRange(0.0, 2.0)
        self._spin_multiplex_settle.setSingleStep(0.05)
        self._spin_multiplex_settle.setValue(0.2)
        self._spin_multiplex_settle.setSuffix(" sec")
        self._spin_multiplex_settle.setToolTip("Пауза после освобождения слота")
        row_settle.addWidget(lbl_settle)
        row_settle.addWidget(self._spin_multiplex_settle)
        mpx_layout.addLayout(row_settle)

        # Backend
        row_backend = QHBoxLayout()
        lbl_backend = QLabel("Бэкенд")
        lbl_backend.setFixedWidth(54)
        lbl_backend.setStyleSheet(f"color: {C.TEXT_DIM}; font-size: 12px;")
        self._combo_multiplex_backend = QComboBox()
        self._combo_multiplex_backend.addItem("V4L2 (быстро)", "v4l2")
        self._combo_multiplex_backend.addItem("OpenCV", "opencv")
        row_backend.addWidget(lbl_backend)
        row_backend.addWidget(self._combo_multiplex_backend)
        mpx_layout.addLayout(row_backend)

        layout.addWidget(grp_mpx)

        # --- Demo Filters ---
        grp_filter = IconGroupBox("Демо-фильтры", str(_ICONS / "palette.svg"))
        filter_layout = grp_filter.body_layout()

        self._combo_filter = QComboBox()
        self._combo_filter.addItems(FILTER_NAMES)
        filter_layout.addWidget(self._combo_filter)

        layout.addWidget(grp_filter)

        # --- Log Viewer ---
        grp_log = IconGroupBox("Логи", str(_ICONS / "clipboard.svg"))
        log_layout = grp_log.body_layout()

        self._log_viewer = QPlainTextEdit()
        self._log_viewer.setReadOnly(True)
        self._log_viewer.setMaximumBlockCount(500)
        log_layout.addWidget(self._log_viewer)

        layout.addWidget(grp_log, stretch=1)

    @staticmethod
    def _make_spin(
        min_val: int, max_val: int, default: int, step: int, suffix: str
    ) -> QSpinBox:
        """Helper to create a consistently styled QSpinBox."""
        spin = QSpinBox()
        spin.setRange(min_val, max_val)
        spin.setSingleStep(step)
        spin.setValue(default)
        spin.setSuffix(suffix)
        return spin

    # -- signal wiring -------------------------------------------------------

    def _connect_signals(self) -> None:
        """Connect all input widgets to the debounce timer."""
        self._spin_width.valueChanged.connect(self._schedule_emit)
        self._spin_height.valueChanged.connect(self._schedule_emit)
        self._spin_fps.valueChanged.connect(self._schedule_emit)
        self._check_hw_accel.stateChanged.connect(self._schedule_emit)
        self._check_sequential.stateChanged.connect(self._on_sequential_changed)
        self._spin_interval.valueChanged.connect(self._schedule_emit)
        self._rtsp_edit.textChanged.connect(self._schedule_emit)
        # Multiplex controls
        self._check_multiplex.stateChanged.connect(self._on_multiplex_changed)
        self._combo_multiplex.currentIndexChanged.connect(self._schedule_emit)
        self._spin_multiplex_slots.valueChanged.connect(self._schedule_emit)
        self._spin_multiplex_dwell.valueChanged.connect(self._schedule_emit)
        self._spin_multiplex_settle.valueChanged.connect(self._schedule_emit)
        self._combo_multiplex_backend.currentIndexChanged.connect(self._schedule_emit)
        # Filter change does NOT restart the manager — it is applied per-frame

    def _schedule_emit(self) -> None:
        """Restart the debounce timer (delays the settings restart)."""
        self._debounce_timer.start()

    def _on_sequential_changed(self, state: int) -> None:
        """Enable/disable the switch interval spin-box and schedule emit."""
        self._spin_interval.setEnabled(bool(state))
        self._schedule_emit()

    def _on_multiplex_changed(self, state: int) -> None:
        """Enable/disable multiplex sub-controls and schedule emit."""
        enabled = bool(state)
        self._combo_multiplex.setEnabled(enabled)
        self._spin_multiplex_slots.setEnabled(enabled)
        self._spin_multiplex_dwell.setEnabled(enabled)
        self._spin_multiplex_settle.setEnabled(enabled)
        self._combo_multiplex_backend.setEnabled(enabled)
        self._schedule_emit()

    def _emit_settings(self) -> None:
        """Emit the ``settings_changed`` signal with current values."""
        self.settings_changed.emit(self.current_settings())
