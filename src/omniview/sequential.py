"""SequentialController — dual-buffer sequential camera switching.

Opens at most two cameras at any time:
  - **Active**: the camera currently displayed / emitting frames.
  - **Buffer**: the next camera, already streaming but not displayed
    (pre-warmed so switching is near-instant).

When the switch interval expires:
  1. The old active camera is released.
  2. The buffer camera becomes the new active.
  3. The next camera in the list is opened as the new buffer.

This avoids the latency of opening a camera at switch time while
never streaming more than two cameras simultaneously.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import cv2

from .threads import BaseCameraThread, build_hw_accel_params

logger = logging.getLogger(__name__)


class SequentialController:
    """Dual-buffer sequential camera rotation.

    Args:
        sources: Ordered list of camera sources.  Each element is either
            an ``int`` (USB camera index) or a ``str`` (RTSP URL).
        switch_interval: Seconds to display each camera before switching.
        frame_callback: Called as ``frame_callback(source_id, frame)`` for
            every frame read from the active camera.
        frame_queue: Optional queue; active frames are ``put`` as
            ``(source_id, frame)`` tuples (used by the GUI bridge).
        width: Desired frame width.
        height: Desired frame height.
        fps: Target FPS.
        hw_acceleration: Request GPU-accelerated decoding.
        settle: Pause (seconds) after releasing a camera before opening
            the next one — gives the USB bus time to free isochronous
            bandwidth.
        show_gui: If True, ``cv2.imshow`` the active camera.
        show_camera_id: If True, overlay the camera ID on the frame.
        window_title: Window title for ``cv2.imshow``.
        exit_keys: Keys that trigger stop.
    """

    def __init__(
        self,
        sources: List[Any],
        switch_interval: float = 5.0,
        frame_callback: Optional[Callable] = None,
        frame_queue: Optional[Any] = None,
        *,
        width: int = 640,
        height: int = 480,
        fps: int = 30,
        hw_acceleration: bool = True,
        settle: float = 0.2,
        show_gui: bool = False,
        show_camera_id: bool = False,
        window_title: str = "Camera Switcher",
        exit_keys: tuple = (ord("q"), 27),
    ) -> None:
        self.sources = list(sources)
        self.switch_interval = switch_interval
        self.frame_callback = frame_callback
        self.frame_queue = frame_queue
        self.width = width
        self.height = height
        self.fps = fps
        self.hw_acceleration = hw_acceleration
        self.settle = settle
        self.show_gui = show_gui
        self.show_camera_id = show_camera_id
        self.window_title = window_title
        self.exit_keys = exit_keys

        self.stop_event = threading.Event()

        # Index into self.sources for the currently active camera.
        self._active_idx: int = 0
        # Open VideoCapture handles: source_id → cap
        self._caps: Dict[Any, cv2.VideoCapture] = {}
        # Which source is currently "active" (displayed) vs "buffer"
        self._active_source: Optional[Any] = None
        self._buffer_source: Optional[Any] = None

        # For the GUI bridge: the currently active source id so it can
        # emit the correct signal.
        self._active_id_lock = threading.Lock()
        self._active_id: Optional[Any] = None

    # -- public API -----------------------------------------------------------

    def start(self) -> None:
        """Open the first two cameras and begin the switching loop.

        This method blocks until ``stop_event`` is set or an exit key is
        pressed.  Run it on a background thread when non-blocking
        behaviour is needed.
        """
        if not self.sources:
            logger.error("No cameras found for sequential mode")
            return

        self._active_idx = 0
        self._open_initial()

        try:
            self._loop()
        except Exception as e:
            logger.error("Sequential mode error: %s", e)
        finally:
            self._release_all()
            if self.show_gui:
                cv2.destroyAllWindows()

    def stop(self) -> None:
        """Signal the controller to stop and release resources."""
        self.stop_event.set()

    def get_active_source(self) -> Optional[Any]:
        """Return the source id of the currently displayed camera.

        Thread-safe: can be called from the GUI poll timer while the
        controller runs on a background thread.
        """
        with self._active_id_lock:
            return self._active_id

    # -- camera opening -------------------------------------------------------

    def _open_camera(self, source: Any) -> Optional[cv2.VideoCapture]:
        """Open a camera with platform-specific backends."""
        if isinstance(source, int):
            backends = BaseCameraThread.DEFAULT_BACKENDS.get(
                "linux" if sys.platform == "linux" else "default"
            )
            for api in backends:
                params = build_hw_accel_params(api, self.hw_acceleration)
                cap = (
                    cv2.VideoCapture(source, api, params)
                    if params
                    else cv2.VideoCapture(source, api)
                )
                if cap.isOpened():
                    self._configure_cap(cap)
                    return cap
                cap.release()
        else:
            # RTSP / file path
            attempts: List[Tuple[int, Optional[list]]] = []
            if self.hw_acceleration:
                attempts.append(
                    (cv2.CAP_FFMPEG,
                     [cv2.CAP_PROP_HW_ACCELERATION,
                      cv2.VIDEO_ACCELERATION_ANY])
                )
            attempts.append((cv2.CAP_FFMPEG, None))
            attempts.append((cv2.CAP_ANY, None))
            for backend, params in attempts:
                try:
                    if params is not None:
                        cap = cv2.VideoCapture(source, backend, params)
                    else:
                        cap = cv2.VideoCapture(source, backend)
                except Exception:
                    continue
                if cap is not None and cap.isOpened():
                    self._configure_cap(cap)
                    return cap
                if cap is not None:
                    cap.release()
        return None

    def _configure_cap(self, cap: cv2.VideoCapture) -> None:
        """Set capture parameters."""
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        cap.set(cv2.CAP_PROP_FPS, self.fps)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    # -- lifecycle helpers ----------------------------------------------------

    def _open_initial(self) -> None:
        """Open the first camera (active) and optionally the second (buffer)."""
        src0 = self.sources[0]
        cap0 = self._open_camera(src0)
        if cap0 is None:
            logger.warning("Cannot open first camera %s", src0)
            return
        self._caps[src0] = cap0
        self._active_source = src0
        self._set_active_id(src0)
        logger.info("Sequential: active camera %s", src0)

        if len(self.sources) > 1:
            src1 = self.sources[1]
            cap1 = self._open_camera(src1)
            if cap1 is not None:
                self._caps[src1] = cap1
                self._buffer_source = src1
                logger.info("Sequential: buffer camera %s", src1)
            # else: buffer failed — will open on next rotation

    def _release_source(self, source: Any) -> None:
        """Release the VideoCapture for *source* and remove from bookkeeping."""
        cap = self._caps.pop(source, None)
        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass
        if self._active_source == source:
            self._active_source = None
        if self._buffer_source == source:
            self._buffer_source = None

    def _release_all(self) -> None:
        """Release every open camera."""
        for cap in self._caps.values():
            try:
                cap.release()
            except Exception:
                pass
        self._caps.clear()
        self._active_source = None
        self._buffer_source = None
        with self._active_id_lock:
            self._active_id = None

    # -- main loop ------------------------------------------------------------

    def _loop(self) -> None:
        """Read frames from the active camera, keep the buffer warm,
        rotate when the interval expires.
        """
        switch_time = time.time()

        while not self.stop_event.is_set():
            # -- Read from active camera --
            if self._active_source is not None:
                cap = self._caps.get(self._active_source)
                if cap is not None and cap.isOpened():
                    ret, frame = cap.read()
                    if ret and frame is not None:
                        self._emit_frame(self._active_source, frame)

            # -- Keep buffer warm (discard frames) --
            if self._buffer_source is not None:
                cap = self._caps.get(self._buffer_source)
                if cap is not None and cap.isOpened():
                    cap.grab()  # discard — just advance the stream

            # -- Check switch interval --
            now = time.time()
            if now - switch_time >= self.switch_interval:
                self._rotate()
                switch_time = now

            # -- Exit key check (GUI mode) --
            if self.show_gui:
                key = cv2.waitKey(1)
                if key in self.exit_keys:
                    self.stop_event.set()
                    break

            # Small sleep to avoid busy-loop when no frame is ready
            time.sleep(0.01)

    def _rotate(self) -> None:
        """Rotate: release active → promote buffer → open new buffer."""
        old_active = self._active_source
        old_buffer = self._buffer_source

        # 1. Release old active
        if old_active is not None:
            self._release_source(old_active)
            if self.settle > 0:
                time.sleep(self.settle)

        # 2. Promote buffer → active
        if old_buffer is not None:
            self._active_source = old_buffer
            self._buffer_source = None
            self._set_active_id(old_buffer)
            logger.debug("Sequential: promoted buffer %s → active",
                         old_buffer)
        else:
            # No buffer (only 1 camera or buffer failed to open) —
            # just advance the index and open next.
            pass

        # 3. Determine the next camera index for the new buffer
        next_idx = self._next_buffer_index()
        if next_idx is not None:
            next_src = self.sources[next_idx]
            if next_src not in self._caps:
                cap = self._open_camera(next_src)
                if cap is not None:
                    self._caps[next_src] = cap
                    self._buffer_source = next_src
                    logger.debug("Sequential: opened buffer %s", next_src)
                else:
                    logger.warning("Sequential: cannot open buffer %s",
                                   next_src)

        # 4. Edge case: if promotion failed and we have no active,
        #    try to open the next camera as active.
        if self._active_source is None:
            next_idx = self._next_buffer_index()
            if next_idx is not None:
                next_src = self.sources[next_idx]
                cap = self._open_camera(next_src)
                if cap is not None:
                    self._caps[next_src] = cap
                    self._active_source = next_src
                    self._set_active_id(next_src)
                    logger.info("Sequential: opened active %s (fallback)",
                                next_src)

        # Update the index tracker to the current active position
        if self._active_source is not None:
            try:
                self._active_idx = self.sources.index(self._active_source)
            except ValueError:
                pass

    def _next_buffer_index(self) -> Optional[int]:
        """Return the index of the next camera to open as buffer.

        Scans forward from the current active index, wrapping around,
        skipping cameras that are already open.
        """
        n = len(self.sources)
        for offset in range(1, n + 1):
            idx = (self._active_idx + offset) % n
            src = self.sources[idx]
            if src not in self._caps:
                return idx
        return None

    # -- frame emission -------------------------------------------------------

    def _emit_frame(self, source: Any, frame: Any) -> None:
        """Send a frame from the active camera to callbacks / queue / GUI."""
        if self.show_gui:
            if self.show_camera_id:
                cv2.putText(
                    frame,
                    f"Camera {source}",
                    (10, 30),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    1,
                    (0, 255, 0),
                    2,
                )
            cv2.imshow(self.window_title, frame)

        if self.frame_callback:
            self.frame_callback(source, frame)

        if self.frame_queue is not None:
            self.frame_queue.put((source, frame))

    # -- thread-safe active id ------------------------------------------------

    def _set_active_id(self, source: Any) -> None:
        with self._active_id_lock:
            self._active_id = source
