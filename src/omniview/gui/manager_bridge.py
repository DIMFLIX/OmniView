"""ManagerBridge — non-blocking adapter between camera managers and Qt.

Architecture
~~~~~~~~~~~~
The library's ``*.start()`` methods are **blocking** calls that
run ``_main_loop`` — a tight CPU busy-loop that consumes the GIL and
makes the GUI unresponsive.  Additionally, the library's *sequential
mode* pushes frames via ``frame_callback`` instead of the
``frame_queue``, which is incompatible with the polling approach.

**Solution**: we bypass ``manager.start()`` entirely and instead:

1. Start ``_monitor_cameras`` daemon threads manually (hot-plug
   detection every 3 s for USB; IP cameras are always available).
2. Drain ``frame_queue`` directly from a QTimer on the GUI thread
   (≈30 Hz).
3. Implement *sequential mode* ourselves by showing only the frames
   from the camera whose turn it is, cycling every
   ``switch_interval`` seconds.

**Dual-manager**: both ``USBCameraManager`` and ``IPCameraManager``
share a single ``frame_queue``.  IP camera IDs are offset by
``IP_ID_OFFSET`` (10 000) to avoid collisions with USB indices (0–9).
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any, Dict, Optional, Set

import numpy as np
from PyQt6.QtCore import QObject, QTimer, pyqtSignal, pyqtSlot

from omniview.managers import IPCameraManager
from omniview.managers import USBCameraManager


# ---------------------------------------------------------------------------
# QLogHandler — thread-safe log buffer drained by the poll timer
# ---------------------------------------------------------------------------


class QLogHandler(logging.Handler):
    """A ``logging.Handler`` that stores records and lets the poll timer
    forward them to the GUI thread safely (no QObject inheritance needed).
    """

    def __init__(self) -> None:
        super().__init__()
        self.setFormatter(
            logging.Formatter("[%(levelname)s] %(name)s: %(message)s")
        )
        self._records: list[str] = []
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        msg = self.format(record)
        with self._lock:
            self._records.append(msg)

    def drain(self) -> list[str]:
        """Return all pending log messages and clear the buffer."""
        with self._lock:
            msgs = self._records[:]
            self._records.clear()
        return msgs


# ---------------------------------------------------------------------------
# ManagerBridge — the public API for the Dashboard
# ---------------------------------------------------------------------------


# Offset applied to IP camera IDs to avoid collision with USB indices.
IP_ID_OFFSET: int = 10_000


class ManagerBridge(QObject):
    """Non-blocking bridge between camera managers and Qt widgets.

    Manages both ``USBCameraManager`` (hot-plug USB) and
    ``IPCameraManager`` (RTSP / video files).  Both share a single
    ``frame_queue`` so the poll timer drains frames from all sources.

    Signals:
        frame_ready(int, np.ndarray):
            A new frame is available for the given camera.
        cameras_changed(set[int]):
            The set of connected camera IDs has changed.
        sequential_camera_changed(int):
            The active camera switched in sequential mode.
        log_message(str):
            A log line from the library.
        restart_complete():
            Emitted after a settings-reload finishes.
    """

    frame_ready = pyqtSignal(int, object)  # camera_id, np.ndarray
    cameras_changed = pyqtSignal(set)  # set of camera IDs
    sequential_camera_changed = pyqtSignal(int)
    log_message = pyqtSignal(str)
    restart_complete = pyqtSignal()
    parked_status = pyqtSignal(dict)  # {camera_id: staleness_seconds}

    def __init__(self, parent=None) -> None:
        super().__init__(parent)

        # USB manager
        self._usb_manager: Optional[USBCameraManager] = None
        self._usb_monitor_thread: Optional[threading.Thread] = None

        # IP manager
        self._ip_manager: Optional[IPCameraManager] = None
        self._ip_monitor_thread: Optional[threading.Thread] = None

        # Shared frame queue (created once, passed to both managers)
        self._frame_queue: queue.Queue = queue.Queue(maxsize=30)

        # Log handlers (one per manager)
        self._log_handlers: list[QLogHandler] = []

        # Poll timer (GUI thread)
        self._poll_timer: Optional[QTimer] = None
        self._prev_camera_ids: Set[int] = set()

        # Sequential mode state
        self._sequential_mode: bool = False
        self._switch_interval: float = 3.0
        self._seq_index: int = 0
        self._seq_switch_time: float = time.time()

        # Whether _start_poll_signal has been connected
        self._poll_signal_connected: bool = False

        # Cached frames for cameras not producing new data this poll
        self._cached_frames: Dict[int, np.ndarray] = {}

        # Restart coordination: prevent concurrent restart() calls
        self._restart_lock = threading.Lock()
        self._restart_pending: Optional[dict] = None
        self._is_restarting: bool = False

    # -- lifecycle -----------------------------------------------------------

    # Signal used to trigger _start_timer on the GUI thread
    _start_poll_signal = pyqtSignal()

    def start(self) -> None:
        """Create managers, start their monitor threads, start polling."""
        self._create_managers()

        # Start USB monitor thread (hot-plug detection)
        if self._usb_manager is not None:
            self._usb_manager.stop_event.clear()
            self._usb_monitor_thread = threading.Thread(
                target=self._usb_manager._monitor_cameras, daemon=True
            )
            self._usb_monitor_thread.start()

        # Start IP monitor thread (keeps IP camera threads alive)
        if self._ip_manager is not None:
            self._ip_manager.stop_event.clear()
            self._ip_monitor_thread = threading.Thread(
                target=self._ip_manager._monitor_cameras, daemon=True
            )
            self._ip_monitor_thread.start()

        # Start the poll timer on the GUI thread (even if start() is
        # called from a background restart thread, the signal ensures the
        # QTimer lives on the correct thread).
        if not self._poll_signal_connected:
            self._start_poll_signal.connect(self._start_poll_timer)
            self._poll_signal_connected = True
        self._start_poll_signal.emit()

    @pyqtSlot()
    def _start_poll_timer(self) -> None:
        """Create and start the poll timer (must run on the GUI thread)."""
        if self._poll_timer is not None:
            self._poll_timer.stop()
        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll)
        self._poll_timer.start(33)

    def stop(self) -> None:
        """Stop all managers and their background threads."""
        # Stop the poll timer first so no more queue drains happen.
        if self._poll_timer is not None:
            self._poll_timer.stop()
            self._poll_timer = None

        # Collect camera threads BEFORE mgr.stop() deletes them from
        # the cameras dict.  We need the Thread refs to join them later.
        camera_threads: list[threading.Thread] = []
        for mgr in (self._usb_manager, self._ip_manager):
            if mgr is not None:
                with mgr.lock:
                    for info in mgr.cameras.values():
                        t = info.get("thread")
                        if t is not None and t.is_alive():
                            camera_threads.append(t)

        # Signal managers to stop (sets stop_event, calls
        # _remove_camera for each device which joins with timeout=1s).
        for mgr in (self._usb_manager, self._ip_manager):
            if mgr is not None:
                mgr.stop()

        # Now drain + join until all threads are dead.
        # Camera threads use blocking frame_queue.put(). When the
        # queue is full and the poll timer (the only consumer) is
        # already stopped, a thread can be stuck inside put()
        # indefinitely — it never checks stop_event, cap.release()
        # never runs, and V4L2 devices stay locked.  Continuously
        # draining the queue unblocks those threads so they notice
        # stop_event and exit.
        monitor_threads = [
            t
            for t in (self._usb_monitor_thread, self._ip_monitor_thread)
            if t is not None
        ]
        all_threads = camera_threads + monitor_threads

        deadline = time.time() + 5.0
        for t in all_threads:
            remaining = max(0.05, deadline - time.time())
            t.join(timeout=remaining)
            # Drain between joins to unblock other threads stuck on put()
            while not self._frame_queue.empty():
                try:
                    self._frame_queue.get_nowait()
                except queue.Empty:
                    break

        # Final aggressive drain
        while not self._frame_queue.empty():
            try:
                self._frame_queue.get_nowait()
            except queue.Empty:
                break

        self._usb_manager = None
        self._ip_manager = None
        self._usb_monitor_thread = None
        self._ip_monitor_thread = None

    def restart(self, **attrs: Any) -> None:
        """Stop the managers, update attributes, and restart.

        Keyword arguments are set on the *new* manager instances that
        ``start()`` creates, ensuring all parameters take effect.

        Runs on a background thread so the GUI stays responsive while
        V4L2 devices release (typically < 2 s).

        Uses a lock to serialize restarts: if a restart is already in
        progress, the new settings are queued and applied by the running
        restart thread after it finishes — no competing stop/start cycles
        that cause cameras to disappear.
        """
        with self._restart_lock:
            self._restart_pending = attrs
            already_running = self._is_restarting

        if already_running:
            # A restart thread is already running; it will pick up
            # _restart_pending on its next loop iteration.
            return

        def _do_restart() -> None:
            while True:
                with self._restart_lock:
                    attrs = self._restart_pending
                    self._restart_pending = None
                    self._is_restarting = True
                if attrs is None:
                    with self._restart_lock:
                        self._is_restarting = False
                    break
                self.stop()
                self._pending_attrs = attrs
                self._prev_camera_ids = set()
                self._cached_frames.clear()
                # Recreate the shared queue to discard stale frames
                self._frame_queue = queue.Queue(maxsize=30)
                # Give V4L2 devices time to fully release after old
                # threads exit.  Without this, the new manager's
                # _get_available_devices() probe finds devices busy
                # (V4L2 allows only one opener) and marks them as
                # unavailable.
                time.sleep(0.3)
                self.start()
                self.restart_complete.emit()
                # Small pause to coalesce rapid-fire settings changes
                time.sleep(0.15)

        threading.Thread(target=_do_restart, daemon=True).start()

    # -- internals -----------------------------------------------------------

    def _create_managers(self) -> None:
        """Instantiate USB and IP managers with a shared frame queue."""
        pending = getattr(self, "_pending_attrs", None) or {}

        # Common params
        frame_width = pending.get("frame_width", 640)
        frame_height = pending.get("frame_height", 480)
        fps = pending.get("fps", 30)
        hw_acceleration = pending.get("hw_acceleration", True)

        # Capture sequential/interval for ourselves
        self._sequential_mode = pending.get("sequential_mode", False)
        self._switch_interval = pending.get("switch_interval", 3.0)

        # --- USB manager ---
        # Multiplex settings
        multiplex_mode = pending.get("multiplex_mode", "auto")
        multiplex_slots = pending.get("multiplex_slots", 2)
        multiplex_dwell = pending.get("multiplex_dwell", 1.5)
        multiplex_settle = pending.get("multiplex_settle", 0.2)
        multiplex_backend = pending.get("multiplex_backend", "v4l2")

        self._usb_manager = USBCameraManager(
            show_gui=False,
            max_cameras=10,
            frame_width=frame_width,
            frame_height=frame_height,
            fps=fps,
            hw_acceleration=hw_acceleration,
            frame_callback=None,
            multiplex_mode=multiplex_mode,
            multiplex_slots=multiplex_slots,
            multiplex_dwell=multiplex_dwell,
            multiplex_settle=multiplex_settle,
            multiplex_backend=multiplex_backend,
        )
        # Replace the default queue with our shared one
        self._usb_manager.frame_queue = self._frame_queue
        self._usb_manager.sequential_mode = False

        # --- IP manager (only if URLs provided) ---
        rtsp_urls: list[str] = pending.get("rtsp_urls", [])
        if rtsp_urls:
            self._ip_manager = IPCameraManager(
                rtsp_urls=rtsp_urls,
                show_gui=False,
                max_cameras=len(rtsp_urls),
                frame_width=frame_width,
                frame_height=frame_height,
                fps=fps,
                hw_acceleration=hw_acceleration,
                frame_callback=None,
            )
            # Replace queue + offset IDs so they don't collide with USB
            self._ip_manager.frame_queue = self._frame_queue

            # Monkey-patch _get_available_devices to return offset IDs
            original_urls = list(rtsp_urls)
            self._ip_manager._get_available_devices = (  # type: ignore[assignment]
                lambda urls=original_urls: [
                    i + IP_ID_OFFSET for i in range(len(urls))
                ]
            )

            # Monkey-patch _create_camera_thread to map offset IDs back
            # to rtsp_urls indices and put offset IDs into the frame queue
            from omniview.threads import IPCameraThread

            original_create = self._ip_manager._create_camera_thread
            ip_mgr = self._ip_manager

            def _create_offset_thread(camera_id: int, stop_event: threading.Event):
                url_idx = camera_id - IP_ID_OFFSET
                thread = IPCameraThread(
                    rtsp_url=ip_mgr.rtsp_urls[url_idx],
                    camera_id=camera_id,  # offset ID → goes into frame_queue
                    frame_queue=ip_mgr.frame_queue,
                    stop_event=stop_event,
                    frame_width=ip_mgr.frame_width,
                    frame_height=ip_mgr.frame_height,
                    fps=ip_mgr.fps,
                    min_uptime=ip_mgr.min_uptime,
                    hw_acceleration=ip_mgr.hw_acceleration,
                )
                return thread

            self._ip_manager._create_camera_thread = _create_offset_thread
        else:
            self._ip_manager = None

        self._pending_attrs = None

        # --- Attach log handlers ---
        self._log_handlers.clear()
        for mgr in (self._usb_manager, self._ip_manager):
            if mgr is None:
                continue
            handler = QLogHandler()
            mgr.logger.addHandler(handler)
            self._log_handlers.append(handler)

    @pyqtSlot()
    def _poll(self) -> None:
        """Called by the poll timer: drain frames + detect camera changes."""
        # 1) Drain shared frame queue directly
        frames: Dict[int, np.ndarray] = {}
        while True:
            try:
                dev_id, frame = self._frame_queue.get_nowait()
                if frame is not None and len(frame.shape) == 3:
                    frames[dev_id] = frame
                    self._cached_frames[dev_id] = frame
            except queue.Empty:
                break

        # 2) Merge with cached frames (show last known frame for idle cams)
        for mgr in (self._usb_manager, self._ip_manager):
            if mgr is None:
                continue
            with mgr.lock:
                for dev_id in list(mgr.cameras.keys()):
                    if dev_id not in frames and dev_id in self._cached_frames:
                        frames[dev_id] = self._cached_frames[dev_id]

        # 2b) Merge multiplexed camera frames (from scheduler, not from threads)
        mpx_scheduler = (
            getattr(self._usb_manager, "_multiplex_scheduler", None)
            if self._usb_manager is not None
            else None
        )
        if mpx_scheduler is not None:
            now = time.time()
            active_mpx = mpx_scheduler.get_active_cameras()
            last_fresh = mpx_scheduler.get_last_fresh()
            parked_info: Dict[int, float] = {}
            for cam_id, frame in mpx_scheduler.get_all_frames().items():
                if frame is not None:
                    # For parked cameras, use the last known frame
                    self._cached_frames[cam_id] = frame
                    frames[cam_id] = frame
                    if cam_id not in active_mpx:
                        lf = last_fresh.get(cam_id)
                        parked_info[cam_id] = (now - lf) if lf else 0.0
            if parked_info:
                self.parked_status.emit(parked_info)

        # 3) Sequential mode: only emit frames for the active camera
        if self._sequential_mode and frames:
            active_ids = sorted(frames.keys())
            if not active_ids:
                return

            now = time.time()
            if now - self._seq_switch_time >= self._switch_interval:
                self._seq_index = (self._seq_index + 1) % len(active_ids)
                self._seq_switch_time = now
                self.sequential_camera_changed.emit(
                    active_ids[self._seq_index % len(active_ids)]
                )

            active_id = active_ids[self._seq_index % len(active_ids)]
            if active_id in frames:
                emit_frames = {active_id: frames[active_id]}
            else:
                emit_frames = {}
        else:
            emit_frames = frames

        # 4) Emit frames
        for cam_id, frame in emit_frames.items():
            self.frame_ready.emit(cam_id, frame)

        # 5) Detect camera set changes (include multiplexed cameras)
        current_ids: Set[int] = set()
        for mgr in (self._usb_manager, self._ip_manager):
            if mgr is not None:
                with mgr.lock:
                    current_ids.update(mgr.cameras.keys())
        if mpx_scheduler is not None:
            current_ids.update(mpx_scheduler.get_multiplex_cameras())
        if current_ids != self._prev_camera_ids:
            self._prev_camera_ids = current_ids
            self.cameras_changed.emit(current_ids)
            self._seq_index = 0
            self._seq_switch_time = time.time()

        # 6) Drain log handlers
        for handler in self._log_handlers:
            for msg in handler.drain():
                self.log_message.emit(msg)
