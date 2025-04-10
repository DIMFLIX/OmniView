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
        """

        super().__init__()
        self.camera_id = camera_id
        self.frame_queue = frame_queue
        self.stop_event = stop_event
        self.frame_width = frame_width
        self.frame_height = frame_height
        self.fps = fps
        self.min_uptime = min_uptime

        self.cap: cv2.VideoCapture | None = None
        self.last_frame_time = 0
        self.retry_count = 0
        self.max_retries = 3
        self.logger = logging.getLogger(f"{self.__class__.__name__}-{camera_id}")

    def _try_open_camera(self, backends: list) -> Optional[cv2.VideoCapture]:
        """A common method for opening a camera with different backends"""
        for backend in backends:
            try:
                cap = cv2.VideoCapture(self._get_open_args(backend), backend)
                if cap.isOpened():
                    self._configure_camera(cap)
                    return cap
            except Exception:
                continue
        return None

    def _configure_camera(self, cap: cv2.VideoCapture):
        """General camera configuration"""
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.frame_width)
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
        try:
            cap = cv2.VideoCapture(self.rtsp_url)
            if cap.isOpened():
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                return cap
        except Exception as e:
            self.logger.error(f"Failed to open IP camera {self.rtsp_url}: {e}")
        return None
