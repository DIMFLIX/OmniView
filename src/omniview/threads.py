import logging
import queue
import sys
import threading
import time
from abc import ABC
from abc import abstractmethod
from typing import Any
from typing import Optional

import cv2

# Capture backends that honor the (open-only) CAP_PROP_HW_ACCELERATION property.
# Other backends (e.g. V4L2, DSHOW) ignore or reject extra params, so the
# acceleration params must not be passed to them.
HW_ACCEL_BACKENDS = (
    cv2.CAP_FFMPEG,
    cv2.CAP_GSTREAMER,
    cv2.CAP_MSMF,
    cv2.CAP_INTEL_MFX,
)

# Human-readable names for the negotiated cv2.VIDEO_ACCELERATION_* values.
_HW_ACCEL_NAMES = {
    cv2.VIDEO_ACCELERATION_NONE: "none (software)",
    cv2.VIDEO_ACCELERATION_ANY: "any",
    cv2.VIDEO_ACCELERATION_D3D11: "d3d11",
    cv2.VIDEO_ACCELERATION_VAAPI: "vaapi",
    cv2.VIDEO_ACCELERATION_MFX: "mfx",
}
# DRM (Raspberry Pi / V4L2 M2M) only exists in newer OpenCV builds.
if hasattr(cv2, "VIDEO_ACCELERATION_DRM"):
    _HW_ACCEL_NAMES[cv2.VIDEO_ACCELERATION_DRM] = "drm"


def supports_hw_acceleration(backend: int) -> bool:
    """Return True if the capture backend honors CAP_PROP_HW_ACCELERATION."""
    return backend in HW_ACCEL_BACKENDS


def build_hw_accel_params(backend: int, enabled: bool) -> list:
    """Build VideoCapture open params requesting hardware acceleration.

    Uses VIDEO_ACCELERATION_ANY so OpenCV picks the platform-appropriate API
    (D3D11 on Windows, VAAPI on Linux, ...) and transparently falls back to
    software decoding when no accelerator is available. Returns an empty list
    when acceleration is disabled or the backend does not support the property,
    so callers can use the plain VideoCapture constructor instead.
    """
    if enabled and supports_hw_acceleration(backend):
        return [cv2.CAP_PROP_HW_ACCELERATION, cv2.VIDEO_ACCELERATION_ANY]
    return []


def hw_acceleration_name(value: float) -> str:
    """Map a negotiated CAP_PROP_HW_ACCELERATION value to a readable name."""
    return _HW_ACCEL_NAMES.get(int(value), f"unknown ({int(value)})")


class BaseCameraThread(threading.Thread, ABC):
    DEFAULT_BACKENDS = {
        "linux": [cv2.CAP_V4L2],
        "default": [cv2.CAP_DSHOW, cv2.CAP_MSMF],
    }

    def __init__(
        self,
        camera_id: int,
        frame_queue: queue.Queue,
        stop_event: threading.Event,
        frame_width: int = 640,
        frame_height: int = 480,
        fps: int = 30,
        min_uptime: float = 5.0,
        hw_acceleration: bool = True,
    ):
        """
        Base thread for handling a single camera stream

        Args:
            camera_id: Unique identifier for the camera
            frame_queue: Queue for sending frames to main thread
            stop_event: Event to signal thread termination
            frame_width: Desired frame width
            frame_height: Desired frame height
            fps: Target frames per second
            min_uptime: Minimum operational time before reconnecting (seconds)
            hw_acceleration: Request GPU-accelerated decoding on capable
                backends (FFMPEG/GStreamer/MSMF/MFX); falls back to software
        """

        super().__init__()
        self.camera_id = camera_id
        self.frame_queue = frame_queue
        self.stop_event = stop_event
        self.frame_width = frame_width
        self.frame_height = frame_height
        self.fps = fps
        self.min_uptime = min_uptime
        self.hw_acceleration = hw_acceleration

        self.cap: cv2.VideoCapture | None = None
        self.last_frame_time = 0
        self.retry_count = 0
        self.max_retries = 3
        self.logger = logging.getLogger(f"{self.__class__.__name__}-{camera_id}")

    def _try_open_camera(self, backends: list) -> Optional[cv2.VideoCapture]:
        """A common method for opening a camera with different backends"""
        for backend in backends:
            try:
                cap = self._create_capture(self._get_open_args(backend), backend)
                if cap.isOpened():
                    self._configure_camera(cap)
                    self._log_acceleration(cap, backend)
                    return cap
            except Exception:
                continue
        return None

    def _create_capture(self, source: Any, backend: int) -> cv2.VideoCapture:
        """Open a VideoCapture, requesting HW acceleration on capable backends."""
        params = build_hw_accel_params(backend, self.hw_acceleration)
        if params:
            return cv2.VideoCapture(source, backend, params)
        return cv2.VideoCapture(source, backend)

    def _log_acceleration(self, cap: cv2.VideoCapture, backend: int):
        """Log the acceleration mode OpenCV negotiated for an opened capture."""
        if not (self.hw_acceleration and supports_hw_acceleration(backend)):
            return
        try:
            mode = hw_acceleration_name(cap.get(cv2.CAP_PROP_HW_ACCELERATION))
        except Exception:
            return
        self.logger.info(f"Camera {self._get_source()} hardware acceleration: {mode}")

    def _configure_camera(self, cap: cv2.VideoCapture):
        """General camera configuration"""
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.frame_width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.frame_height)
        cap.set(cv2.CAP_PROP_FPS, self.fps)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        if hasattr(self, "_additional_config"):
            self._additional_config(cap)

    @abstractmethod
    def _get_open_args(self, backend: int) -> Any:
        """Получить аргументы для открытия камеры"""

    @abstractmethod
    def _get_source(self) -> str:
        """Получить идентификатор камеры"""

    def run(self):
        """Main thread loop for camera processing"""
        while not self.stop_event.is_set() and self.retry_count < self.max_retries:
            source = self._get_source()
            try:
                self.cap = self._open_camera()
                if not self.cap or not self.cap.isOpened():
                    raise RuntimeError(f"Cannot open camera {source}")

                self._process_camera_stream(source)
            except Exception as e:
                self._handle_camera_error(source, e)
            finally:
                self._release_camera_resources()

    def _open_camera(self) -> Optional[cv2.VideoCapture]:
        """Открытие камеры с учетом платформы"""
        backends = self.DEFAULT_BACKENDS.get(
            "linux" if sys.platform == "linux" else "default"
        )
        return self._try_open_camera(backends)

    def _process_camera_stream(self, source: str):
        """Continuously read and process frames from camera"""
        self.retry_count = 0
        self.logger.info(f"Camera {source} started")
        start_time = time.time()

        while not self.stop_event.is_set():
            ret, frame = self.cap.read()
            if not ret:
                if time.time() - start_time < self.min_uptime:
                    self.logger.warning(f"Camera {source} frame read error")
                    time.sleep(0.1)
                    continue
                break

            self.frame_queue.put((self.camera_id, frame))
            self.last_frame_time = time.time()

    def _handle_camera_error(self, source: str, error: Exception):
        """Handle camera errors and schedule reconnection"""
        self.logger.error(f"Camera {source} error: {str(error)}")
        self.retry_count += 1
        if self.retry_count < self.max_retries:
            self.logger.info(f"Reconnecting to {source}...")
            time.sleep(2.0)

    def _release_camera_resources(self):
        """Clean up camera resources"""
        if self.cap and self.cap.isOpened():
            self.cap.release()
        self.cap = None


class USBCameraThread(BaseCameraThread):
    def __init__(self, *args, **kwargs):
        """
        Base thread for handling a single USB camera stream

        Args:
            camera_id: Unique identifier for the camera
            frame_queue: Queue for sending frames to main thread
            stop_event: Event to signal thread termination
            frame_width: Desired frame width
            frame_height: Desired frame height
            fps: Target frames per second
            min_uptime: Minimum operational time before reconnecting (seconds)
        """
        super().__init__(*args, **kwargs)

    def _get_open_args(self, _) -> Any:
        return self.camera_id

    def _get_source(self) -> str:
        return f"USB Camera {self.camera_id}"

    def _additional_config(self, cap: cv2.VideoCapture):
        cap.set(cv2.CAP_PROP_AUTOFOCUS, 0)


class IPCameraThread(BaseCameraThread):
    def __init__(self, rtsp_url: str, *args, **kwargs):
        """
        Base thread for handling a single IP camera stream

        Args:
            rtsp_url: RTSP stream URL
            camera_id: Unique identifier for the camera
            frame_queue: Queue for sending frames to main thread
            stop_event: Event to signal thread termination
            frame_width: Desired frame width
            frame_height: Desired frame height
            fps: Target frames per second
            min_uptime: Minimum operational time before reconnecting (seconds)
        """
        super().__init__(*args, **kwargs)
        self.rtsp_url = rtsp_url

    def _get_open_args(self, _) -> Any:
        return self.rtsp_url

    def _get_source(self) -> str:
        return self.rtsp_url

    def _open_camera(self) -> Optional[cv2.VideoCapture]:
        # Prefer FFMPEG with hardware acceleration (best for H.264/H.265 RTSP
        # decoding), then fall back to plain FFMPEG, then auto-detected backend.
        attempts = []
        if self.hw_acceleration:
            attempts.append(
                (
                    cv2.CAP_FFMPEG,
                    [cv2.CAP_PROP_HW_ACCELERATION, cv2.VIDEO_ACCELERATION_ANY],
                )
            )
        attempts.append((cv2.CAP_FFMPEG, None))
        attempts.append((cv2.CAP_ANY, None))

        for backend, params in attempts:
            try:
                if params is not None:
                    cap = cv2.VideoCapture(self.rtsp_url, backend, params)
                else:
                    cap = cv2.VideoCapture(self.rtsp_url, backend)
            except Exception as e:
                self.logger.error(
                    f"Failed to open IP camera {self.rtsp_url} (backend={backend}): {e}"
                )
                continue

            if cap is not None and cap.isOpened():
                self._configure_camera(cap)
                self._log_acceleration(cap, backend)
                return cap
            if cap is not None:
                cap.release()
        return None
