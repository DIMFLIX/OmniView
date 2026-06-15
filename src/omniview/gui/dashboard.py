"""Dashboard — the main QMainWindow for OmniView Dashboard.

Layout:
    QSplitter (75% | 25%)
    ├── QScrollArea → QWidget → QGridLayout  (dynamic camera grid)
    └── SettingsPanel                            (config + logs)

Hot-plug flow:
    ManagerBridge.cameras_changed → _on_cameras_changed
        → create/remove CameraWidget instances in the grid

Frame flow:
    ManagerBridge.frame_ready → CameraWidget.update_frame

Settings hot-reload:
    SettingsPanel.settings_changed → _apply_settings
        → show overlay → bridge.restart() → hide overlay
"""

from __future__ import annotations

from typing import Dict, Optional, Set

import numpy as np
from PyQt6.QtCore import Qt, pyqtSlot
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import (
    QGridLayout,
    QLabel,
    QMainWindow,
    QScrollArea,
    QSplitter,
    QWidget,
)

from .camera_widget import CameraWidget
from .manager_bridge import IP_ID_OFFSET, ManagerBridge
from .settings_panel import SettingsPanel
from .theme import NO_CAMERA_STYLE, OVERLAY_STYLE


class Dashboard(QMainWindow):
    """Main application window — hot-plug, zero-config camera dashboard."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("OmniView Dashboard")
        self.resize(1280, 720)

        # Core objects
        self._bridge = ManagerBridge(self)
        self._settings = SettingsPanel()
        self._camera_widgets: Dict[int, CameraWidget] = {}
        self._fullscreen_camera_id: Optional[int] = None
        self._sequential_mode: bool = False
        self._seq_active_camera_id: Optional[int] = None
        self._prev_grid_rows: int = 0
        self._prev_grid_cols: int = 0

        # Build UI
        self._build_ui()
        self._connect_signals()

        # Overlay for settings-restart
        self._overlay: Optional[QWidget] = None

        # Start immediately — zero-config
        self._bridge.start()

    # -- UI construction -----------------------------------------------------

    def _build_ui(self) -> None:
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # LEFT: scroll area with grid
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)

        self._grid_container = QWidget()
        self._grid_layout = QGridLayout(self._grid_container)
        self._grid_layout.setSpacing(6)
        self._grid_layout.setContentsMargins(6, 6, 6, 6)

        self._no_camera_label = QLabel("Камеры не обнаружены")
        self._no_camera_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._no_camera_label.setStyleSheet(NO_CAMERA_STYLE)
        self._grid_layout.addWidget(self._no_camera_label, 0, 0, 1, 1, Qt.AlignmentFlag.AlignCenter)

        self._scroll.setWidget(self._grid_container)
        splitter.addWidget(self._scroll)

        # RIGHT: settings panel
        splitter.addWidget(self._settings)

        # 75% / 25% split
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([960, 320])

        self.setCentralWidget(splitter)

    # -- signal wiring -------------------------------------------------------

    def _connect_signals(self) -> None:
        self._bridge.frame_ready.connect(self._on_frame_ready)
        self._bridge.cameras_changed.connect(self._on_cameras_changed)
        self._bridge.sequential_camera_changed.connect(
            self._on_sequential_camera_changed
        )
        self._bridge.log_message.connect(self._settings.append_log)
        self._bridge.restart_complete.connect(self._on_restart_complete)
        self._bridge.parked_status.connect(self._on_parked_status)
        self._settings.settings_changed.connect(self._apply_settings)
        self._settings._combo_filter.currentTextChanged.connect(
            self._on_filter_changed
        )

    # -- slots: frames -------------------------------------------------------

    @pyqtSlot(int, object)
    def _on_frame_ready(self, camera_id: int, frame: np.ndarray) -> None:
        """Forward a frame to the corresponding CameraWidget."""
        if self._sequential_mode:
            # In sequential mode only the active camera's tile is visible
            if camera_id == self._seq_active_camera_id:
                widget = self._camera_widgets.get(camera_id)
                if widget is not None:
                    widget.update_frame(frame)
        else:
            widget = self._camera_widgets.get(camera_id)
            if widget is not None:
                widget.update_frame(frame)

    # -- slots: camera hot-plug ----------------------------------------------

    @pyqtSlot(set)
    def _on_cameras_changed(self, camera_ids: Set[int]) -> None:
        """Synchronize CameraWidgets with the current camera set."""
        current = set(self._camera_widgets.keys())
        added = camera_ids - current
        removed = current - camera_ids

        for cid in added:
            self._add_camera_widget(cid)
        for cid in removed:
            self._remove_camera_widget(cid)

        # In sequential mode: fix active camera if it was removed
        if self._sequential_mode:
            if (self._seq_active_camera_id is not None
                    and self._seq_active_camera_id not in camera_ids):
                ids = sorted(camera_ids)
                self._seq_active_camera_id = ids[0] if ids else None
            elif self._seq_active_camera_id is None and camera_ids:
                ids = sorted(camera_ids)
                self._seq_active_camera_id = ids[0]

        self._rebuild_grid()

        # Show / hide "no cameras" label — remove from layout when hidden
        if camera_ids:
            self._grid_layout.removeWidget(self._no_camera_label)
            self._no_camera_label.hide()
        else:
            self._grid_layout.addWidget(self._no_camera_label, 0, 0, 1, 1, Qt.AlignmentFlag.AlignCenter)
            self._no_camera_label.show()

    def _add_camera_widget(self, camera_id: int) -> None:
        """Create a CameraWidget and wire its double_clicked signal."""
        widget = CameraWidget(camera_id)
        widget.set_filter(self._settings.current_filter())
        # IP cameras get a human-readable label from their URL
        if camera_id >= IP_ID_OFFSET:
            idx = camera_id - IP_ID_OFFSET
            urls = self._settings._parse_rtsp_urls()
            if idx < len(urls):
                url = urls[idx]
                # Show last two path segments or host
                short = url.split("?")[0].rstrip("/")
                if "/" in short:
                    short = short.rsplit("/", 1)[-1]
                if "@" in short:
                    short = short.rsplit("@", 1)[-1]
                label = f"IP {idx}: {short}"
            else:
                label = f"IP {idx}"
            widget.set_display_label(label)
        widget.double_clicked.connect(self._on_camera_double_clicked)
        self._camera_widgets[camera_id] = widget

    def _remove_camera_widget(self, camera_id: int) -> None:
        """Remove and delete the CameraWidget for the given camera."""
        widget = self._camera_widgets.pop(camera_id, None)
        if widget is not None:
            self._grid_layout.removeWidget(widget)
            widget.deleteLater()

        if self._fullscreen_camera_id == camera_id:
            self._fullscreen_camera_id = None

    # -- grid layout ---------------------------------------------------------

    def _rebuild_grid(self) -> None:
        """Reposition all CameraWidgets in the grid according to count."""
        # Remove all widgets from layout first
        for widget in self._camera_widgets.values():
            self._grid_layout.removeWidget(widget)

        # Clear old stretch factors from previous layout
        for r in range(self._prev_grid_rows):
            self._grid_layout.setRowStretch(r, 0)
        for c in range(self._prev_grid_cols):
            self._grid_layout.setColumnStretch(c, 0)

        ids = sorted(self._camera_widgets.keys())
        count = len(ids)

        # Handle sequential mode — single tile spanning the whole area
        if self._sequential_mode:
            active_id = self._seq_active_camera_id
            if active_id is not None and active_id in self._camera_widgets:
                self._grid_layout.addWidget(
                    self._camera_widgets[active_id], 0, 0
                )
                self._grid_layout.setRowStretch(0, 1)
                self._grid_layout.setColumnStretch(0, 1)
                self._camera_widgets[active_id].show()
                for cid, w in self._camera_widgets.items():
                    if cid != active_id:
                        w.hide()
            self._prev_grid_rows = 1
            self._prev_grid_cols = 1
            self._invalidate_grid()
            return

        # Handle fullscreen mode (double-click)
        if self._fullscreen_camera_id is not None:
            if self._fullscreen_camera_id in self._camera_widgets:
                self._grid_layout.addWidget(
                    self._camera_widgets[self._fullscreen_camera_id], 0, 0
                )
                self._grid_layout.setRowStretch(0, 1)
                self._grid_layout.setColumnStretch(0, 1)
                self._camera_widgets[self._fullscreen_camera_id].show()
                for cid, w in self._camera_widgets.items():
                    if cid != self._fullscreen_camera_id:
                        w.hide()
            self._prev_grid_rows = 1
            self._prev_grid_cols = 1
            self._invalidate_grid()
            return

        # Normal grid mode — all visible, equal cell sizes
        cols = self._grid_columns(count)
        rows = (count + cols - 1) // cols if cols > 0 else 0
        for idx, cid in enumerate(ids):
            row = idx // cols
            col = idx % cols
            self._grid_layout.addWidget(self._camera_widgets[cid], row, col)
            # Every cell gets equal stretch so all tiles are the same size
            self._grid_layout.setRowStretch(row, 1)
            self._grid_layout.setColumnStretch(col, 1)
            self._camera_widgets[cid].show()

        self._prev_grid_rows = rows
        self._prev_grid_cols = cols
        self._invalidate_grid()

    def _invalidate_grid(self) -> None:
        """Force the grid container and layout to recalculate."""
        self._grid_layout.invalidate()
        self._grid_container.updateGeometry()
        self._grid_container.update()

    @staticmethod
    def _grid_columns(count: int) -> int:
        """Determine the number of columns for the grid layout.

        Always max 2 columns — grid grows downward with scrolling.
        Layout examples: 1→[1], 2→[2], 3→[2,1], 4→[2,2], 5→[2,2,1] ...
        """
        if count <= 1:
            return 1
        return 2

    # -- slots: sequential camera switch ------------------------------------

    @pyqtSlot(int)
    def _on_sequential_camera_changed(self, camera_id: int) -> None:
        """Switch the displayed camera tile in sequential mode."""
        self._seq_active_camera_id = camera_id
        self._rebuild_grid()

    # -- slots: multiplex parked status --------------------------------------

    @pyqtSlot(dict)
    def _on_parked_status(self, parked_info: dict) -> None:
        """Update parked badges on CameraWidgets for multiplexed cameras."""
        for cam_id, staleness in parked_info.items():
            widget = self._camera_widgets.get(cam_id)
            if widget is not None:
                widget.set_parked(True, staleness)
        # Clear parked status for cameras that are no longer parked
        for cam_id, widget in self._camera_widgets.items():
            if cam_id not in parked_info:
                widget.set_parked(False)

    # -- slots: double-click fullscreen toggle -------------------------------

    @pyqtSlot(int)
    def _on_camera_double_clicked(self, camera_id: int) -> None:
        """Toggle fullscreen for the double-clicked camera widget."""
        if self._fullscreen_camera_id == camera_id:
            # Exit fullscreen
            self._camera_widgets[camera_id].set_fullscreen(False)
            self._fullscreen_camera_id = None
        else:
            # Exit old fullscreen first
            if self._fullscreen_camera_id is not None:
                old = self._camera_widgets.get(self._fullscreen_camera_id)
                if old is not None:
                    old.set_fullscreen(False)
            # Enter new fullscreen
            self._fullscreen_camera_id = camera_id
            self._camera_widgets[camera_id].set_fullscreen(True)
        self._rebuild_grid()

    # -- slots: filter change ------------------------------------------------

    @pyqtSlot(str)
    def _on_filter_changed(self, filter_name: str) -> None:
        """Propagate the selected filter to all CameraWidgets."""
        for widget in self._camera_widgets.values():
            widget.set_filter(filter_name)

    # -- slots: settings hot-reload ------------------------------------------

    @pyqtSlot(dict)
    def _apply_settings(self, settings: dict) -> None:
        """Restart the manager with new settings; show overlay meanwhile."""
        # Track sequential mode locally (bridge handles it after restart)
        new_seq = settings.get("sequential_mode", False)
        seq_changed = new_seq != self._sequential_mode
        self._sequential_mode = new_seq

        if seq_changed:
            if new_seq:
                # Entering sequential: set active camera to the first one
                ids = sorted(self._camera_widgets.keys())
                self._seq_active_camera_id = ids[0] if ids else None
            else:
                # Leaving sequential: reset
                self._seq_active_camera_id = None
            self._rebuild_grid()

        self._show_overlay("Применение настроек...")
        # restart() runs on a background thread — does not block the GUI
        self._bridge.restart(**settings)

    @pyqtSlot()
    def _on_restart_complete(self) -> None:
        """Hide the overlay after the manager has restarted."""
        self._hide_overlay()

    # -- overlay -------------------------------------------------------------

    def _show_overlay(self, text: str) -> None:
        """Show a semi-transparent overlay over the video area."""
        if self._overlay is not None:
            return

        self._overlay = QLabel(text, self._scroll)
        self._overlay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._overlay.setStyleSheet(OVERLAY_STYLE)
        self._overlay.setGeometry(self._scroll.viewport().rect())
        self._overlay.raise_()
        self._overlay.show()

    def _hide_overlay(self) -> None:
        """Remove the overlay."""
        if self._overlay is not None:
            self._overlay.deleteLater()
            self._overlay = None

    # -- lifecycle -----------------------------------------------------------

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        """Stop the manager before the window closes."""
        self._bridge.stop()
        super().closeEvent(event)
