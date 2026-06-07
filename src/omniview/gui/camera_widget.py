"""CameraWidget — a QLabel that displays a live video frame with overlay.

Features:
- Stores its own ``camera_id`` for identification.
- Draws the camera ID and current FPS directly onto the frame via
  ``cv2.putText`` before converting to ``QPixmap``.
- Emits ``double_clicked`` on a mouse double-click so the parent
  dashboard can toggle fullscreen mode.
- Hover effect: highlights border when the mouse is over the tile.
"""

from __future__ import annotations

import time

import cv2
import numpy as np
from PyQt6.QtCore import Qt
from PyQt6.QtCore import pyqtSignal
from PyQt6.QtGui import QImage
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import QLabel
from PyQt6.QtWidgets import QSizePolicy

from .theme import CAMERA_WIDGET_FULLSCREEN_STYLE
from .theme import CAMERA_WIDGET_HOVER_STYLE
from .theme import CAMERA_WIDGET_STYLE
from .theme import C


class CameraWidget(QLabel):
    """A QLabel subclass that renders a live camera frame with metadata overlay."""

    double_clicked = pyqtSignal(int)  # emits camera_id

    def __init__(self, camera_id: int, parent=None) -> None:
        super().__init__(parent)
        self.camera_id = camera_id
        self._display_label: str = f"Cam {camera_id}"
        self._frame_count: int = 0
        self._fps: float = 0.0
        self._fps_time: float = time.time()
        self._current_filter_name: str = "Original"
        self._is_fullscreen: bool = False

        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(320, 240)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        self.setStyleSheet(CAMERA_WIDGET_STYLE)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    # -- public API ----------------------------------------------------------

    def set_display_label(self, label: str) -> None:
        """Set the text shown in the overlay (e.g. 'Cam 0' or an RTSP URL)."""
        self._display_label = label

    def set_filter(self, name: str) -> None:
        """Switch the active display filter by name."""
        self._current_filter_name = name

    def set_fullscreen(self, enabled: bool) -> None:
        """Toggle the fullscreen border highlight."""
        self._is_fullscreen = enabled
        self.setStyleSheet(
            CAMERA_WIDGET_FULLSCREEN_STYLE if enabled else CAMERA_WIDGET_STYLE
        )

    def update_frame(self, frame: np.ndarray) -> None:
        """Apply the current filter, draw overlays, convert to QPixmap and display.

        Args:
            frame: BGR ``np.ndarray`` from the camera thread.
        """
        from .filters import get_filter  # deferred to avoid circular at import

        # Apply filter
        filtered = get_filter(self._current_filter_name)(frame)

        # Compute FPS
        self._frame_count += 1
        now = time.time()
        elapsed = now - self._fps_time
        if elapsed >= 1.0:
            self._fps = self._frame_count / elapsed
            self._frame_count = 0
            self._fps_time = now

        # Draw overlay (camera ID + FPS) directly on the BGR frame
        self._draw_overlay(filtered)

        # BGR -> RGB for Qt
        rgb = cv2.cvtColor(filtered, cv2.COLOR_BGR2RGB)
        h, w, ch = rgb.shape
        bytes_per_line = ch * w
        qimg = QImage(rgb.data, w, h, bytes_per_line, QImage.Format.Format_RGB888)
        pixmap = QPixmap.fromImage(qimg)

        # Scale to widget size while keeping aspect ratio
        scaled = pixmap.scaled(
            self.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.setPixmap(scaled)

    # -- internals -----------------------------------------------------------

    def _draw_overlay(self, frame: np.ndarray) -> None:
        """Draw camera ID and FPS text onto the frame in-place."""
        # Semi-transparent background for readability
        overlay = frame.copy()
        cv2.rectangle(overlay, (0, 0), (200, 70), (0, 0, 0), -1)
        cv2.addWeighted(overlay, 0.5, frame, 0.5, 0, frame)

        cv2.putText(
            frame,
            self._display_label,
            (10, 28),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            (88, 166, 255),  # ACCENT-ish blue
            2,
            cv2.LINE_AA,
        )
        fps_color = (63, 185, 80) if self._fps > 15 else (210, 153, 34)
        cv2.putText(
            frame,
            f"{self._fps:.1f} FPS",
            (10, 58),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            fps_color,
            2,
            cv2.LINE_AA,
        )

    # -- mouse events --------------------------------------------------------
    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        """Emit ``double_clicked`` with this widget's camera_id."""
        self.double_clicked.emit(self.camera_id)
        super().mouseDoubleClickEvent(event)

    def enterEvent(self, event) -> None:  # noqa: N802
        """Highlight border on hover (unless fullscreen)."""
        if not self._is_fullscreen:
            self.setStyleSheet(CAMERA_WIDGET_HOVER_STYLE)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        """Restore normal border."""
        if not self._is_fullscreen:
            self.setStyleSheet(CAMERA_WIDGET_STYLE)
        super().leaveEvent(event)
