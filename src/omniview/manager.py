import logging
import os
import queue
import sys
import threading
import time
from typing import Any
from collections.abc import Callable

import cv2


class CameraThread(threading.Thread):
    def __init__(
        self,
        camera_id: int,
        frame_queue: queue.Queue,
        stop_event: threading.Event,
        use_ip_camera: bool,
        rtsp_url: str | None = None,
        frame_width: int = 640,
        frame_height: int = 480,
        fps: int = 30,
        min_uptime: float = 5.0,
    ):
        """
        Thread for handling a single camera stream

        Args:
            camera_id: Unique identifier for the camera
            frame_queue: Queue for sending frames to main thread
            stop_event: Event to signal thread termination
            use_ip_camera: Whether to use IP camera (RTSP) or USB camera
            rtsp_url: RTSP stream URL (only for IP cameras)
            frame_width: Desired frame width
            frame_height: Desired frame height
            fps: Target frames per second
            min_uptime: Minimum operational time before reconnecting (seconds)
        """
        super().__init__()
        self.camera_id = camera_id
        self.frame_queue = frame_queue
        self.stop_event = stop_event
        self.use_ip_camera = use_ip_camera
        self.rtsp_url = rtsp_url
        self.frame_width = frame_width
        self.frame_height = frame_height
        self.fps = fps
        self.min_uptime = min_uptime

        self.cap = None
        self.last_frame_time = 0
        self.retry_count = 0
        self.max_retries = 3
        self.logger = logging.getLogger(f"CameraThread-{camera_id}")

    def _open_camera(self) -> cv2.VideoCapture | None:
        """Initialize and configure the camera capture"""
        if self.use_ip_camera:
            return self._open_ip_camera()
        return self._open_usb_camera()

    def _open_ip_camera(self) -> cv2.VideoCapture | None:
        """Initialize IP camera using RTSP stream"""
        if not self.rtsp_url:
            return None

        try:
            cap = cv2.VideoCapture(self.rtsp_url)
            if cap.isOpened():
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                return cap
        except Exception as e:
            self.logger.error(f"Failed to open IP camera {self.rtsp_url}: {e}")
        return None

    def _open_usb_camera(self) -> cv2.VideoCapture | None:
        """Initialize USB camera with platform-specific settings"""
        if sys.platform == "linux":
            attempts = [lambda: cv2.VideoCapture(self.camera_id, cv2.CAP_V4L2)]
        else:
            attempts = [
                lambda: cv2.VideoCapture(self.camera_id, cv2.CAP_DSHOW),
                lambda: cv2.VideoCapture(self.camera_id, cv2.CAP_MSMF),
            ]

        for attempt in attempts:
            try:
                cap = attempt()
                if cap.isOpened():
                    cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.frame_width)
                    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.frame_height)
                    cap.set(cv2.CAP_PROP_FPS, self.fps)
                    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                    cap.set(cv2.CAP_PROP_AUTOFOCUS, 0)
                    return cap
            except Exception:
                continue
        return None

    def _get_source(self) -> str:
        """Get human-readable camera identifier"""
        return self.rtsp_url if self.use_ip_camera else f"USB Camera {self.camera_id}"

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


class CameraManager:
    def __init__(
        self,
        use_ip_cameras: bool = False,
        ip_cameras: list[str] | None = None,
        show_gui: bool = True,
        max_cameras: int = 10,
        frame_width: int = 640,
        frame_height: int = 480,
        fps: int = 30,
        min_uptime: float = 5.0,
        frame_callback: Callable[[int, Any], None] | None = None,
        exit_keys: tuple = (ord("q"), 27),
    ):
        """
        Manager for handling multiple camera streams

        Args:
            use_ip_cameras: Whether to use IP cameras (RTSP) or USB cameras
            ip_cameras: List of RTSP stream URLs for IP cameras
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

        self.use_ip_cameras = use_ip_cameras
        self.ip_cameras = ip_cameras or []
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
        self.cameras = {}
        self.frame_queue = queue.Queue(maxsize=self.max_cameras * 2)

        if self.show_gui and sys.platform == "linux":
            os.environ["QT_QPA_PLATFORM"] = "xcb"

    def _setup_logging(self):
        """Configure logging settings"""
        self.logger = logging.getLogger("CameraManager")
        self.logger.setLevel(logging.INFO)
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("[%(levelname)s] %(name)s: %(message)s"))
        self.logger.addHandler(handler)

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

    def _get_available_devices(self) -> list[int]:
        """Get list of available camera devices"""
        if self.use_ip_cameras:
            return list(range(len(self.ip_cameras)))

        devices = []
        for i in range(self.max_cameras):
            try:
                if sys.platform == "linux":
                    dev_path = f"/dev/video{i}"
                    if os.path.exists(dev_path):
                        try:
                            with open(
                                f"/sys/class/video4linux/video{i}/name") as f:
                                if "camera" in f.read().lower():
                                    devices.append(i)
                        except Exception:
                            devices.append(i)
                else:
                    devices.append(i)
            except Exception as e:
                self.logger.warning(f"Device check error for {i}: {str(e)}")
        return devices

    def _monitor_cameras(self):
        """Continuously monitor and update camera connections"""
        while not self.stop_event.is_set():
            current_devices = self._get_available_devices()

            with self.lock:
                self._update_camera_connections(current_devices)

            time.sleep(3)

    def _update_camera_connections(self, current_devices: list[int]):
        """Add or remove cameras based on availability"""
        # Add newly connected cameras
        for dev_id in current_devices:
            if dev_id not in self.cameras:
                self._add_camera(dev_id)

        # Remove disconnected cameras
        for dev_id in list(self.cameras.keys()):
            if self._should_remove_camera(dev_id, current_devices):
                self._remove_camera(dev_id)

    def _should_remove_camera(self, dev_id: int, current_devices: list[int]) -> bool:
        """Determine if a camera should be removed"""
        if self.use_ip_cameras:
            return not self.cameras[dev_id]["thread"].is_alive()
        return (
            dev_id not in current_devices
            and not self.cameras[dev_id]["thread"].is_alive()
        )

    def _add_camera(self, dev_id: int):
        """Initialize and start a new camera thread"""
        if dev_id in self.cameras:
            return

        source = self.ip_cameras[dev_id] if self.use_ip_cameras else dev_id
        self.logger.info(f"Adding camera {source}")

        try:
            stop_event = threading.Event()
            thread = CameraThread(
                camera_id=dev_id,
                frame_queue=self.frame_queue,
                stop_event=stop_event,
                use_ip_camera=self.use_ip_cameras,
                rtsp_url=self.ip_cameras[dev_id] if self.use_ip_cameras else None,
                frame_width=self.frame_width,
                frame_height=self.frame_height,
                fps=self.fps,
                min_uptime=self.min_uptime,
            )

            self.cameras[dev_id] = {
                "thread": thread,
                "stop_event": stop_event,
                "last_frame": None,
                "last_update": 0,
                "source": source,
            }

            thread.start()
        except Exception as e:
            self.logger.error(f"Error adding camera {source}: {str(e)}")

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
        """Generate window title for camera display"""
        camera_type = "IP" if self.use_ip_cameras else "USB"
        source = (
            self.cameras[dev_id]["source"] if dev_id in self.cameras else str(dev_id)
        )
        return f"Camera {dev_id} ({camera_type}): {source}"

    def process_frames(self) -> dict[int, Any]:
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

    def _add_cached_frames(self, frames: dict[int, Any]):
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

    def _update_gui_windows(self, frames: dict[int, Any]):
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
