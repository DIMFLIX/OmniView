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
from typing import Set

import cv2

from .multiplex import MultiplexScheduler
from .threads import BaseCameraThread
from .threads import IPCameraThread
from .threads import USBCameraThread
from .threads import build_hw_accel_params
from .usb_topology import present_capture_devices
from .usb_topology import present_video_devices


class BaseCameraManager(ABC):
    def __init__(
        self,
        show_gui: bool = False,
        show_camera_id: bool = False,
        max_cameras: int = 10,
        frame_width: int = 640,
        frame_height: int = 480,
        fps: int = 30,
        min_uptime: float = 5.0,
        frame_callback: Optional[Callable[[int, Any], None]] = None,
        exit_keys: tuple = (ord("q"), 27),
        hw_acceleration: bool = True,
    ):
        """
        Base manager for handling multiple camera streams

        Args:
            show_gui: Display video windows
            show_camera_id: Adds a caption with the camera ID to the frame
            max_cameras: Maximum number of cameras to handle
            frame_width: Desired frame width
            frame_height: Desired frame height
            fps: Target frames per second
            min_uptime: Minimum operational time before reconnecting (seconds)
            frame_callback: Callback function for frame processing
            exit_keys: Keyboard keys to exit the application
            hw_acceleration: Request GPU-accelerated decoding when available
                (uses D3D11 on Windows, VAAPI on Linux); falls back to software
        """
        self._setup_logging()

        self.show_gui = show_gui
        self.show_camera_id = show_camera_id
        self.max_cameras = max_cameras
        self.frame_width = frame_width
        self.frame_height = frame_height
        self.fps = fps
        self.min_uptime = min_uptime
        self.frame_callback = frame_callback
        self.exit_keys = exit_keys
        self.hw_acceleration = hw_acceleration

        self.active_windows = set()
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.cameras: dict[int, dict] = {}
        self.frame_queue = queue.Queue(maxsize=self.max_cameras * 2)

        if self.show_gui and sys.platform == "linux":
            # Force the xcb (X11/XWayland) Qt plugin bundled with opencv-python
            # wheels, which usually lack a native Wayland plugin. setdefault lets
            # users with a Wayland-capable Qt build override it via the env.
            os.environ.setdefault("QT_QPA_PLATFORM", "xcb")

    def _setup_logging(self):
        """Configure logging settings"""
        self.logger = logging.getLogger(self.__class__.__name__)
        self.logger.setLevel(logging.INFO)
        # logging.getLogger(name) returns a process-wide shared instance, so
        # adding a handler unconditionally attaches a new one for every
        # manager created in the same process — duplicating every log line
        # once per instance (the cause of the repeated log output). Attach
        # our handler only once and disable propagation so a configured root
        # logger can't emit a second copy either.
        if not self.logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(
                logging.Formatter("[%(levelname)s] %(name)s: %(message)s")
            )
            self.logger.addHandler(handler)
        self.logger.propagate = False

    @abstractmethod
    def _get_available_devices(self) -> List[int]:
        pass

    @abstractmethod
    def _create_camera_thread(
        self, camera_id: int, stop_event: threading.Event
    ) -> threading.Thread:
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

        scheduler = getattr(self, "_multiplex_scheduler", None)
        if scheduler is not None:
            scheduler.stop()
            self._multiplex_scheduler = None

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

            # HighGUI (Qt) is not thread-safe: destroying a window off the
            # main thread triggers "QObject::killTimer/startTimer: Timers
            # cannot be (stopped|started) from another thread" and can
            # SIGSEGV. _remove_camera runs both on the main thread (stop())
            # and on the background _monitor_cameras thread (hot-plug
            # removal), so only touch GUI when on the main thread. Windows
            # orphaned by a background removal are reaped by the main loop's
            # _cleanup_inactive_windows / _cleanup_gui_resources.
            if self.show_gui and threading.current_thread() is threading.main_thread():
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
        if dev_id not in self.cameras:
            # Frame from a multiplexed camera — scheduler manages its state
            if self.frame_callback:
                self.frame_callback(dev_id, frame)
            return
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

    def _show_camera_id_in_frame(self, frame, camera_id: int):
        """Adds a caption with the camera number to the frame"""
        cv2.putText(
            frame,
            f"Camera {camera_id}",
            (10, 30),
            cv2.FONT_HERSHEY_SIMPLEX,
            1,
            (0, 255, 0),
            2,
        )

    def _update_gui_windows(self, frames: Dict[int, Any]):
        """Update all GUI windows with current frames"""
        for dev_id, frame in frames.items():
            try:
                window_title = self._get_window_title(dev_id)
                if self.show_camera_id:
                    self._show_camera_id_in_frame(frame, dev_id)
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


class SequentialCameraMixin:
    """A mixin that adds sequential camera switching functionality to camera managers.

    This mixin allows cameras to be displayed one by one in a cyclic order,
    with a configurable switch interval. It's designed to work with camera managers
    inheriting from `BaseCameraManager`.

    Requires the host class to implement:
        Attributes:
            - frame_callback: Optional[Callable]
            - stop_event: threading.Event
            - cameras_list: List[int]
            - current_cam_idx: int
            - exit_keys: tuple
            - cap: cv2.VideoCapture
            - frame_width: int
            - frame_height: int
            - fps: int
            - hw_acceleration: bool
            - show_gui: bool
            - show_camera_id: bool
            - window_title: str
            - switch_interval: float

        Methods:
            - _get_available_devices()
            - _show_camera_id_in_frame()
    """

    def _open_camera(self, camera_id: int) -> Optional[cv2.VideoCapture]:
        """Open camera with platform-specific parameters."""
        backends = ["linux"] if sys.platform == "linux" else ["default"]
        for backend in backends:
            for api in BaseCameraThread.DEFAULT_BACKENDS[backend]:
                params = build_hw_accel_params(api, self.hw_acceleration)
                if params:
                    cap = cv2.VideoCapture(camera_id, api, params)
                else:
                    cap = cv2.VideoCapture(camera_id, api)
                if cap.isOpened():
                    return cap
        return None

    def _sequential_main_loop(self):
        """Main loop for sequential camera switching"""
        self.cameras_list = self._get_available_devices()
        if not self.cameras_list:
            self.logger.error("No USB cameras found")
            return

        self.logger.info(f"Available cameras: {self.cameras_list}")

        try:
            while not self.stop_event.is_set():
                camera_id = self.cameras_list[self.current_cam_idx]
                success = self._process_camera(camera_id)

                if not success and not self.stop_event.is_set():
                    self.logger.warning(f"Skipping camera {camera_id}")

                self.current_cam_idx = (self.current_cam_idx + 1) % len(
                    self.cameras_list
                )

        except Exception as e:
            self.logger.error(f"Sequential mode error: {str(e)}")
        finally:
            self._cleanup_sequential()

    def _check_exit_keys(self):
        """Handle exit key presses"""
        key = cv2.waitKey(1)
        if key in self.exit_keys:
            self.stop_event.set()

    def _process_camera(self, camera_id: int) -> bool:
        """Process one camera for switch_interval duration"""
        self.cap = self._open_camera(camera_id)
        if not self.cap or not self.cap.isOpened():
            return False

        try:
            self._configure_camera()
            start_time = time.time()

            while not self.stop_event.is_set():
                self._handle_frame(camera_id)
                if self._check_switch_time(start_time):
                    break

            return True
        except Exception as e:
            self.logger.error(f"Camera {camera_id} error: {str(e)}")
            return False
        finally:
            self.cap.release()
            self.cap = None

    def _configure_camera(self):
        """Set camera parameters"""
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.frame_width)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.frame_height)
        self.cap.set(cv2.CAP_PROP_FPS, self.fps)
        self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    def _handle_frame(self, camera_id: int):
        """Read and process single frame"""
        ret, frame = self.cap.read()
        if not ret:
            return

        if self.show_gui:
            self._display_frame(camera_id, frame)

        if self.frame_callback:
            self.frame_callback(camera_id, frame)

        self._check_exit_keys()

    def _display_frame(self, camera_id: int, frame):
        """Show frame in GUI window"""
        if self.show_camera_id:
            self._show_camera_id_in_frame(frame, camera_id)

        cv2.imshow(self.window_title, frame)

    def _check_switch_time(self, start_time: float) -> bool:
        """Check if switch interval has elapsed"""
        return (time.time() - start_time) >= self.switch_interval

    def _cleanup_sequential(self):
        """Final cleanup for sequential mode"""
        if self.cap and self.cap.isOpened():
            self.cap.release()
        if self.show_gui:
            cv2.destroyAllWindows()
        self.stop()


class USBCameraManager(SequentialCameraMixin, BaseCameraManager):
    """
    Manager for handling multiple USB camera streams

    Args:
        show_gui: Display video windows
        show_camera_id: Adds a caption with the camera ID to the frame
        max_cameras: Maximum number of cameras to handle
        frame_width: Desired frame width
        frame_height: Desired frame height
        fps: Target frames per second
        min_uptime: Minimum operational time before reconnecting (seconds)
        frame_callback: Callback function for frame processing
        exit_keys: Keyboard keys to exit the application
        hw_acceleration: Request GPU-accelerated decoding when available
        sequential_mode: Method to show the cameras one by one
        switch_interval: The time after which the cameras will change. Only works if sequential_mode is selected
        multiplex_mode: How to handle USB bus contention:
            "auto" - detect from USB topology (default)
            "off"  - never multiplex
            "force" - multiplex all cameras as if they share one hub
        multiplex_slots: Max simultaneous streams per hub (K, default 2)
        multiplex_dwell: Seconds a camera stays live before rotating out (default 1.5)
        multiplex_settle: Pause after releasing a camera before opening next (default 0.2)
        multiplex_backend: Rotation backend - "v4l2" (STREAMON/OFF) or "opencv" (release/open)
        multiplex_fourcc: Pixel format for V4L2 backend (default "MJPG")
    """

    def __init__(
        self,
        *args,
        sequential_mode: bool = False,
        switch_interval: float = 5.0,
        multiplex_mode: str = "auto",
        multiplex_slots: int = 2,
        multiplex_dwell: float = 1.5,
        multiplex_settle: float = 0.2,
        multiplex_backend: str = "v4l2",
        multiplex_fourcc: str = "MJPG",
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.sequential_mode = sequential_mode
        self.switch_interval = switch_interval
        self.current_cam_idx = 0
        self.cameras_list = []
        self.cap = None
        self.window_title = "USB Camera Switcher"

        self.multiplex_mode = multiplex_mode
        self.multiplex_slots = multiplex_slots
        self.multiplex_dwell = multiplex_dwell
        self.multiplex_settle = multiplex_settle
        self.multiplex_backend = multiplex_backend
        self.multiplex_fourcc = multiplex_fourcc

        # Multiplex scheduler (created in start() after device discovery)
        self._multiplex_scheduler: Optional[MultiplexScheduler] = None

        # Per-camera thread restart counter (sysfs-based detection never
        # removes a present device, so dead threads must be restarted;
        # cap prevents infinite ENOSPC restart loop on a congested hub).
        self._thread_restarts: Dict[int, int] = {}
        self._MAX_THREAD_RESTARTS = 2

        # Cameras that exceeded the restart limit — they are physically
        # present (sysfs sees them) but should NOT be re-added as per-camera
        # threads.  The multiplex scheduler will claim them on the next
        # reconfigure pass; if multiplex is off, they stay condemned until
        # the device node disappears.
        self._condemned_cameras: Set[int] = set()

        # Cameras already announced as multiplex-managed.  _add_camera runs
        # on every monitor scan (~3 s) and multiplexed cameras never enter
        # self.cameras, so logging the "managed by multiplex scheduler"
        # line unconditionally repeats it forever.  Track announced cameras
        # to log the message once per camera.
        self._multiplex_announced: Set[int] = set()

    def start(self):
        """Start camera processing in selected mode"""
        if self.sequential_mode:
            self._sequential_main_loop()
        else:
            super().start()

    def _monitor_cameras(self):
        """Continuously monitor and update camera connections.

        Overrides BaseCameraManager to add multiplex scheduler initialization
        and polling in the monitor loop.

        When multiplex is active, the loop runs at 50 ms for responsive
        rotation. Device presence is read from sysfs
        (``/sys/class/video4linux``) so the check never conflicts with the
        multiplex scheduler's open V4L2 file descriptors.  The old
        ``_get_available_devices()`` probe (``cv2.VideoCapture``) would fail
        for devices the scheduler already has open, causing a 3-second
        oscillation cycle.

        Only *capture-capable* nodes are considered: modern UVC cameras
        expose extra metadata ``/dev/videoN`` nodes that cannot be opened
        for capture, and treating them as cameras spawns doomed threads
        (endless open failures + restart loops) and miscounts cameras in
        the USB topology grouping.

        Sysfs scanning is cheap (no device opens), so it runs every 3 s
        alongside the multiplex topology re-evaluation.
        """
        mpx_initialized = False
        last_scan = 0.0
        cached_present: List[int] = []

        while not self.stop_event.is_set():
            now = time.time()

            # Scan for new/removed cameras every 3 s via sysfs.
            # Unlike the old _get_available_devices() which tried to open
            # each /dev/videoN (conflicting with the multiplex scheduler),
            # present_capture_devices() reads /sys/class/video4linux and
            # keeps only capture-capable nodes — a non-intrusive presence
            # check that never fights open V4L2 fds and skips metadata nodes.
            if not mpx_initialized or now - last_scan >= 3.0:
                sysfs_present = present_capture_devices()
                if sysfs_present is not None:
                    # Linux: use sysfs (always reliable, never conflicts)
                    cached_present = sorted(sysfs_present)
                else:
                    # Non-Linux fallback: probe with cv2.VideoCapture
                    cached_present = self._get_available_devices()
                last_scan = now

                with self.lock:
                    if not mpx_initialized:
                        self._init_multiplex(cached_present)
                        mpx_initialized = True
                    else:
                        # Re-evaluate multiplex topology on each scan so
                        # hot-plugged cameras on a congested hub are
                        # picked up by the scheduler instead of spawning
                        # per-camera threads that hit ENOSPC.
                        self._reconfigure_multiplex(cached_present)
                    self._update_camera_connections(cached_present)

                # Drop multiplexed cameras whose device nodes vanished
                # (e.g. the USB hub was unplugged).  Done on the slow scan
                # cadence so cameras disappear "after a certain time".
                # Reuse the sysfs present set we already read above.
                self._prune_disconnected_multiplex(sysfs_present)

            # Poll the multiplex scheduler (grab frames + rotate windows)
            if self._multiplex_scheduler is not None:
                self._multiplex_scheduler.poll()

            # When multiplex is active, poll at 50 ms for responsive
            # rotation. Otherwise 3 s is fine (just hot-plug detection).
            time.sleep(0.05 if self._multiplex_scheduler is not None else 3)

    def process_frames(self) -> Dict[int, Any]:
        """Drain queued frames, then merge multiplex-scheduler frames.

        Multiplexed cameras are owned by the ``MultiplexScheduler``, not
        ``self.cameras``, so the base-class queue drain + 5 s cache never
        keeps their GUI windows alive between the intermittent frames the
        scheduler emits (only ``slots`` cameras stream at once; parked
        cameras emit nothing). Without this merge each multiplexed window is
        created and destroyed on alternating main-loop iterations, so the
        windows appear to open and instantly close.

        Merging the scheduler's last-known frame for every multiplexed
        camera keeps one stable window per camera (live for active cams,
        parked for the rest), mirroring what ``ManagerBridge`` already does
        for the dashboard. Fresh frames drained from the queue this tick
        take precedence over the scheduler's stored copy.
        """
        frames = super().process_frames()

        scheduler = self._multiplex_scheduler
        if scheduler is not None:
            for dev_id, frame in scheduler.get_all_frames().items():
                if frame is not None and dev_id not in frames:
                    frames[dev_id] = frame

        return frames

    def _get_available_devices(self) -> List[int]:
        devices = []
        backend_key = "linux" if sys.platform == "linux" else "default"
        backend = BaseCameraThread.DEFAULT_BACKENDS[backend_key][0]

        for i in range(self.max_cameras):
            if self._probe_camera(i, backend):
                devices.append(i)
            else:
                self.logger.info(f"The camera with index {i} is not available")
        return devices

    def _probe_camera(self, index: int, backend: int) -> bool:
        """Return True if a camera index can be opened, retrying once.

        Probing several cameras on the same congested USB 2.0 hub
        back-to-back can fail transiently: releasing a UVC device does not
        free its isochronous bandwidth reservation instantly, so opening
        the next device immediately afterwards may report ENOSPC even
        though only one camera is ever streamed at a time.  Without a
        settle pause the affected camera is dropped from the discovered
        list for the whole session — the cause of one camera never
        appearing in sequential mode even though cameras are opened one at
        a time.  Always release the handle (even on failure, to avoid
        leaking the fd) and retry once after a short settle so every
        physically present camera is found.
        """
        settle = max(self.multiplex_settle, 0.0)
        for attempt in range(2):
            cap = cv2.VideoCapture(index, backend)
            opened = cap.isOpened()
            cap.release()
            if opened:
                # Let the bus release this camera's bandwidth before the
                # caller probes/streams the next device.
                if settle:
                    time.sleep(settle)
                return True
            if attempt == 0 and settle:
                time.sleep(settle)
        return False

    def _init_multiplex(self, devices: List[int]) -> List[int]:
        """Set up the multiplex scheduler and return cameras it manages.

        Cameras returned by this method are handled by the MultiplexScheduler
        (they share a congested USB hub) and should NOT have USBCameraThread
        started for them.  All other cameras are unrestricted and go through
        the normal per-camera thread path.
        """
        # Sequential mode and multiplex are mutually exclusive.  Sequential
        # opens one camera at a time, so there is no USB bus contention and
        # the rotation scheduler is unnecessary.
        if self.multiplex_mode == "off" or self.sequential_mode:
            return []

        self._multiplex_scheduler = MultiplexScheduler(
            frame_queue=self.frame_queue,
            width=self.frame_width,
            height=self.frame_height,
            fps=self.fps,
            fourcc=self.multiplex_fourcc,
            hw_acceleration=self.hw_acceleration,
        )
        multiplex_cams = self._multiplex_scheduler.configure(
            devices,
            mode=self.multiplex_mode,
            slots=self.multiplex_slots,
            dwell=self.multiplex_dwell,
            settle=self.multiplex_settle,
            backend=self.multiplex_backend,
        )
        if multiplex_cams:
            self._multiplex_scheduler.start()
        else:
            self._multiplex_scheduler = None
        return multiplex_cams

    def _prune_disconnected_multiplex(
        self, sysfs_present: Optional[Set[int]] = None
    ):
        """Drop multiplexed cameras whose device nodes have disappeared.

        Multiplexed cameras live in the scheduler, not ``self.cameras``, so
        the normal hot-unplug path (thread death + device scan) never removes
        them.  Without this a parked camera keeps showing its last frame
        forever after the USB hub is pulled.  Presence is read from sysfs so
        the check does not fight the scheduler over the busy device nodes it
        is streaming; on platforms without sysfs it is skipped.

        Args:
            sysfs_present: the set already read by the monitor loop, or
                ``None`` to read it fresh (fallback for callers outside the
                loop).
        """
        scheduler = self._multiplex_scheduler
        if scheduler is None:
            return
        if sysfs_present is None:
            sysfs_present = present_video_devices()
        if sysfs_present is None:
            return
        removed = scheduler.sync_available(sysfs_present)
        if removed:
            self.logger.info(
                f"Removed disconnected multiplexed cameras {sorted(removed)}"
            )
            # Reset announcement so a re-plugged camera is logged again.
            self._multiplex_announced.difference_update(removed)

    def _reconfigure_multiplex(self, devices: List[int]):
        """Re-evaluate multiplex topology after hot-plug changes.

        Called on each 3 s scan so that cameras newly connected to a
        congested hub are handed off from their per-camera threads to the
        multiplex scheduler (avoiding ENOSPC), and cameras that left a
        congested hub get their threads back.

        If the scheduler does not yet exist (e.g. no congested hub was
        present at startup) but topology now shows contention (a USB hub
        was hot-plugged with 3+ cameras), the scheduler is created from
        scratch and the congested cameras' threads are stopped.
        """
        if self.multiplex_mode == "off" or self.sequential_mode:
            return

        scheduler = self._multiplex_scheduler

        # No scheduler yet — check if topology now requires multiplexing
        # (e.g. a USB hub was plugged in after program start).
        if scheduler is None:
            from .usb_topology import needs_multiplexing
            _, _, mpx_cams = needs_multiplexing(
                devices, mode=self.multiplex_mode,
                default_slots=self.multiplex_slots,
            )
            if not mpx_cams:
                return  # still no contention
            # Stop per-camera threads for congested cameras FIRST so their
            # V4L2 fds are released before the scheduler tries to open them.
            for dev_id in mpx_cams:
                if dev_id in self.cameras:
                    self._remove_camera(dev_id)
                    self.logger.info(
                        f"Camera {dev_id} stopped for new multiplex scheduler"
                    )
                # The scheduler will own this camera now — un-condemn it.
                self._condemned_cameras.discard(dev_id)
            # Now create the scheduler — it can open the freed devices.
            self._init_multiplex(devices)
            return

        added, removed = scheduler.reconfigure(devices)

        # Cameras that left the scheduler may become per-camera threads
        # again — reset their announcement so a future hand-off re-logs.
        self._multiplex_announced.difference_update(removed)

        # Stop per-camera threads for cameras now managed by the scheduler
        for dev_id in added:
            if dev_id in self.cameras:
                self._remove_camera(dev_id)
                self.logger.info(
                    f"Camera {dev_id} handed off to multiplex scheduler"
                )
            # The scheduler now owns this camera — un-condemn it so it
            # won't be skipped if it ever leaves the scheduler later.
            self._condemned_cameras.discard(dev_id)

        # Cameras removed from multiplex will be picked up by
        # _update_camera_connections on the next iteration.

    def _update_camera_connections(self, current_devices: List[int]):
        """Add, remove, or restart cameras based on sysfs presence.

        Overrides BaseCameraManager to handle the sysfs-based detection
        model.  With sysfs, a camera whose device node exists will always
        appear in ``current_devices`` even if a V4L2 fd is already held by
        the multiplex scheduler.  This means the base class's
        ``_should_remove_camera`` never fires for a present device with a
        dead thread (device present → in list → not removed).  We must
        explicitly restart dead threads for devices that are still
        physically present.

        Restart is limited to ``_MAX_THREAD_RESTARTS`` attempts.  A camera
        on a congested USB hub will keep dying with ENOSPC; without a cap
        it would be restarted every 3 s scan forever.  After the cap is
        hit the camera is removed from ``self.cameras`` and left for the
        multiplex scheduler to pick up on the next reconfigure pass.
        """
        # Add newly connected cameras
        for dev_id in current_devices:
            if dev_id not in self.cameras:
                self._add_camera(dev_id)

        # Remove cameras whose device nodes have disappeared
        # (sysfs says gone) OR restart cameras whose threads died
        # but the device is still physically present.
        for dev_id in list(self.cameras.keys()):
            if dev_id not in current_devices:
                # Device gone — remove regardless of thread state
                if not self.cameras[dev_id]["thread"].is_alive():
                    self._remove_camera(dev_id)
                # Also clear condemned status — the device is gone,
                # there's nothing to hand off to the scheduler anymore.
                self._condemned_cameras.discard(dev_id)
            else:
                # Device present but thread is dead — decide whether to
                # restart or give up (the multiplex scheduler will claim
                # it on the next reconfigure pass).
                if not self.cameras[dev_id]["thread"].is_alive():
                    restarts = self._thread_restarts.get(dev_id, 0)
                    if restarts < self._MAX_THREAD_RESTARTS:
                        self._thread_restarts[dev_id] = restarts + 1
                        self.logger.info(
                            f"Camera {dev_id} thread dead, restarting "
                            f"(attempt {restarts + 1}/{self._MAX_THREAD_RESTARTS})"
                        )
                        self._remove_camera(dev_id)
                        self._add_camera(dev_id)
                    else:
                        self.logger.info(
                            f"Camera {dev_id} thread dead, max restarts "
                            f"reached — condemning (multiplex will claim it)"
                        )
                        self._remove_camera(dev_id)
                        self._condemned_cameras.add(dev_id)

    def _add_camera(self, dev_id: int):
        """Initialize and start a new camera thread (skip multiplexed cams)."""
        if dev_id in self.cameras:
            return
        # Skip cameras managed by the multiplex scheduler.  This runs on
        # every monitor scan (~3 s) and multiplexed cameras never enter
        # self.cameras, so only log the line the first time per camera to
        # avoid spamming the log indefinitely.
        if (self._multiplex_scheduler is not None
                and dev_id in self._multiplex_scheduler.get_multiplex_cameras()):
            if dev_id not in self._multiplex_announced:
                self.logger.info(f"Camera {dev_id} managed by multiplex scheduler")
                self._multiplex_announced.add(dev_id)
            return
        # Skip condemned cameras — they exceeded the restart limit and
        # are waiting for the multiplex scheduler to claim them.
        if dev_id in self._condemned_cameras:
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
        """Stop and remove a camera thread, clearing its restart counter."""
        super()._remove_camera(dev_id)
        # Clear restart counter — the camera is gone from self.cameras
        # now (either handed to scheduler, physically removed, or
        # abandoned after max restarts).
        self._thread_restarts.pop(dev_id, None)

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
            hw_acceleration=self.hw_acceleration,
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
        hw_acceleration: Request GPU-accelerated decoding when available
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
            hw_acceleration=self.hw_acceleration,
        )
