import logging
import os
import queue
import sys
import threading
import time
from abc import ABC
from abc import abstractmethod
from typing import Any
from typing import Callable
from typing import Dict
from typing import List
from typing import Optional

import cv2

from .threads import IPCameraThread
from .threads import USBCameraThread


class BaseCameraManager(ABC):
    def __init__(
        self,
        show_gui: bool = True,
        max_cameras: int = 10,
        frame_width: int = 640,
        frame_height: int = 480,
        fps: int = 30,
        min_uptime: float = 5.0,
        frame_callback: Optional[Callable[[int, Any], None]] = None,
        exit_keys: tuple = (ord("q"), 27),
    ):
        """
        Base manager for handling multiple camera streams

        Args:
            show_gui: Display video windows
            max_cameras: Maximum number of cameras to handle
            frame_width: Desired frame width
            frame_height: Desired frame height
            fps: Target frames per second
            min_uptime: Minimum operational time before reconnecting (seconds)
            frame_callback: Callback function for frame processing
            exit_keys: Keyboard keys to exit the application
        """
        self._setup_logging()

        self.show_gui = show_gui
        self.max_cameras = max_cameras
        self.frame_width = frame_width
        self.frame_height = frame_height
        self.fps = fps
        self.min_uptime = min_uptime
        self.frame_callback = frame_callback
        self.exit_keys = exit_keys

        self.active_windows = set()
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.cameras: dict[int, dict] = {}
        self.frame_queue = queue.Queue(maxsize=self.max_cameras * 2)

        if self.show_gui and sys.platform == "linux":
            os.environ["QT_QPA_PLATFORM"] = "xcb"

    def _setup_logging(self):
        """Configure logging settings"""
        self.logger = logging.getLogger(self.__class__.__name__)
        self.logger.setLevel(logging.INFO)
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("[%(levelname)s] %(name)s: %(message)s"))
        self.logger.addHandler(handler)

    @abstractmethod
    def _get_available_devices(self) -> List[int]:
        pass

    @abstractmethod
    def _create_camera_thread(self, camera_id: int) -> threading.Thread:
        pass

    def start(self):
        """Start the camera manager and begin processing"""
        self.monitor_thread = threading.Thread(
            target=self._monitor_cameras, daemon=True
        )
        self.monitor_thread.start()
        self._main_loop()

    def stop(self):
        """Stop all camera processing and clean up resources"""
        self.stop_event.set()

        for dev_id in list(self.cameras.keys()):
            self._remove_camera(dev_id)

        if hasattr(self, "monitor_thread"):
            self.monitor_thread.join(timeout=1.0)

        self._cleanup_gui_resources()

    def _cleanup_gui_resources(self):
        """Clean up GUI-related resources"""
        if not self.show_gui:
            return

        for window in list(self.active_windows):
            try:
                cv2.destroyWindow(window)
                cv2.waitKey(1)
            except Exception:
                pass
        self.active_windows.clear()

    def _monitor_cameras(self):
        """Continuously monitor and update camera connections"""
        while not self.stop_event.is_set():
            current_devices = self._get_available_devices()

            with self.lock:
                self._update_camera_connections(current_devices)

            time.sleep(3)

    def _update_camera_connections(self, current_devices: List[int]):
        """Add or remove cameras based on availability"""
        # Add newly connected cameras
        for dev_id in current_devices:
            if dev_id not in self.cameras:
                self._add_camera(dev_id)

        # Remove disconnected cameras
        for dev_id in list(self.cameras.keys()):
            if self._should_remove_camera(dev_id, current_devices):
                self._remove_camera(dev_id)

    def _should_remove_camera(self, dev_id: int, current_devices: List[int]) -> bool:
        """Determine if a camera should be removed"""
        return (
            dev_id not in current_devices
            and not self.cameras[dev_id]["thread"].is_alive()
        )

    def _add_camera(self, dev_id: int):
        """Initialize and start a new camera thread"""
        if dev_id in self.cameras:
            return

        self.logger.info(f"Adding camera {dev_id}")

        try:
            stop_event = threading.Event()
            thread = self._create_camera_thread(dev_id, stop_event)

            self.cameras[dev_id] = {
                "thread": thread,
                "stop_event": stop_event,
                "last_frame": None,
                "last_update": 0,
                "source": thread._get_source(),
            }

            thread.start()
        except Exception as e:
            self.logger.error(f"Error adding camera {dev_id}: {str(e)}")

    def _remove_camera(self, dev_id: int):
        """Stop and remove a camera thread"""
        if dev_id not in self.cameras:
            return

        source = self.cameras[dev_id]["source"]
        try:
            self.logger.info(f"Removing camera {source}")
            self.cameras[dev_id]["stop_event"].set()
            self.cameras[dev_id]["thread"].join(timeout=1.0)

            if self.show_gui:
                window_title = self._get_window_title(dev_id)
                if window_title in self.active_windows:
                    cv2.destroyWindow(window_title)
                    self.active_windows.remove(window_title)
                    cv2.waitKey(1)

        except Exception as e:
            self.logger.error(f"Error removing camera {dev_id}: {str(e)}")
        finally:
            if dev_id in self.cameras:
                del self.cameras[dev_id]

    def _get_window_title(self, dev_id: int) -> str:
        camera_type = self.__class__.__name__.replace("CameraManager", "")
        source = (
            self.cameras[dev_id]["source"] if dev_id in self.cameras else str(dev_id)
        )
        return f"Camera {dev_id} ({camera_type}): {source}"

    def process_frames(self) -> Dict[int, Any]:
        """Process all available frames from the queue"""
        frames = {}

        while not self.frame_queue.empty():
            try:
                dev_id, frame = self.frame_queue.get_nowait()
                if frame is not None and len(frame.shape) == 3:
                    frames[dev_id] = frame
                    self._update_camera_state(dev_id, frame)
            except queue.Empty:
                break

        self._add_cached_frames(frames)
        return frames

    def _update_camera_state(self, dev_id: int, frame: Any):
        """Update camera state with new frame"""
        self.cameras[dev_id]["last_frame"] = frame
        self.cameras[dev_id]["last_update"] = time.time()

        if self.frame_callback:
            self.frame_callback(dev_id, frame)

    def _add_cached_frames(self, frames: Dict[int, Any]):
        """Add cached frames from inactive cameras"""
        with self.lock:
            for dev_id in list(self.cameras.keys()):
                if (
                    dev_id not in frames
                    and self.cameras[dev_id]["last_frame"] is not None
                    and time.time() - self.cameras[dev_id]["last_update"] < 5.0
                ):
                    frames[dev_id] = self.cameras[dev_id]["last_frame"]

    def _main_loop(self):
        """Main processing loop"""
        try:
            while not self.stop_event.is_set():
                try:
                    self._process_frame_iteration()
                except Exception as e:
                    self.logger.error(f"Main loop error: {e}")
                    time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def _process_frame_iteration(self):
        """Process one iteration of the main loop"""
        frames = self.process_frames()

        if self.show_gui:
            self._update_gui_windows(frames)

        if self._check_exit_condition():
            self.stop_event.set()

    def _update_gui_windows(self, frames: Dict[int, Any]):
        """Update all GUI windows with current frames"""
        for dev_id, frame in frames.items():
            try:
                window_title = self._get_window_title(dev_id)
                cv2.imshow(window_title, frame)
                self.active_windows.add(window_title)
            except Exception as e:
                self.logger.error(f"Display error for camera {dev_id}: {e}")

        self._cleanup_inactive_windows(frames.keys())

    def _cleanup_inactive_windows(self, active_ids: set):
        """Remove windows for inactive cameras"""
        for window_title in list(self.active_windows):
            dev_id = int(window_title.split()[1])
            if dev_id not in active_ids:
                try:
                    cv2.destroyWindow(window_title)
                    self.active_windows.remove(window_title)
                    cv2.waitKey(1)
                except Exception:
                    pass

    def _check_exit_condition(self) -> bool:
        """Check if exit condition is met"""
        if not self.show_gui:
            return False

        key = cv2.waitKey(1)
        return key in self.exit_keys


class USBCameraManager(BaseCameraManager):
    """
    Manager for handling multiple USB camera streams

    Args:
        show_gui: Display video windows
        max_cameras: Maximum number of cameras to handle
        frame_width: Desired frame width
        frame_height: Desired frame height
        fps: Target frames per second
        min_uptime: Minimum operational time before reconnecting (seconds)
        frame_callback: Callback function for frame processing
        exit_keys: Keyboard keys to exit the application
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def _get_available_devices(self) -> List[int]:
        devices = []
        for i in range(self.max_cameras):
            try:
                if sys.platform == "linux":
                    dev_path = f"/dev/video{i}"
                    if os.path.exists(dev_path):
                        devices.append(i)
                else:
                    devices.append(i)
            except Exception:
                continue
        return devices

    def _create_camera_thread(
        self, camera_id: int, stop_event: threading.Event
    ) -> threading.Thread:
        return USBCameraThread(
            camera_id=camera_id,
            frame_queue=self.frame_queue,
            stop_event=stop_event,
            frame_width=self.frame_width,
            frame_height=self.frame_height,
            fps=self.fps,
            min_uptime=self.min_uptime,
        )


class IPCameraManager(BaseCameraManager):
    """
    Manager for handling multiple IP camera streams

    Args:
        rtsp_urls: RTSP stream URLs
        show_gui: Display video windows
        max_cameras: Maximum number of cameras to handle
        frame_width: Desired frame width
        frame_height: Desired frame height
        fps: Target frames per second
        min_uptime: Minimum operational time before reconnecting (seconds)
        frame_callback: Callback function for frame processing
        exit_keys: Keyboard keys to exit the application
    """

    def __init__(self, rtsp_urls: List[str], *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.rtsp_urls = rtsp_urls

    def _get_available_devices(self) -> List[int]:
        return list(range(len(self.rtsp_urls)))

    def _create_camera_thread(
        self, camera_id: int, stop_event: threading.Event
    ) -> threading.Thread:
        return IPCameraThread(
            rtsp_url=self.rtsp_urls[camera_id],
            camera_id=camera_id,
            frame_queue=self.frame_queue,
            stop_event=stop_event,
            frame_width=self.frame_width,
            frame_height=self.frame_height,
            fps=self.fps,
            min_uptime=self.min_uptime,
        )
