"""Time-multiplexed camera rotation — rolling-window scheduler.

When N USB cameras share a bus that can only sustain K < N simultaneous
isochronous streams, this module rotates a "window" of K live slots across
all N cameras so every camera gets serviced over time.  Cameras not
currently in the active window are "parked" — they show their last captured
frame.

Two backends are supported:

* **V4L2** (preferred): uses ``STREAMON`` / ``STREAMOFF`` on already-open
  devices — rotation takes milliseconds instead of the ~560 ms of a full
  ``release()`` / ``open()`` cycle.
* **OpenCV**: uses ``VideoCapture.release()`` / ``VideoCapture()`` — slower
  but works without the raw V4L2 backend.

Architecture
------------
``MultiplexGroup`` manages one hub's cameras (one rolling window).
``MultiplexScheduler`` owns zero or more groups and provides a unified
interface for the camera manager.
"""

from __future__ import annotations

import logging
import os
import queue
import select
import sys
import threading
import time
from collections import OrderedDict, deque
from typing import Dict, List, Optional, Set, Tuple

import cv2
import numpy as np

from .usb_topology import needs_multiplexing
from .v4l2_backend import V4L2Camera

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# MultiplexGroup — one rolling window for one hub
# ---------------------------------------------------------------------------

class MultiplexGroup:
    """Rolling-window multiplexer for cameras that share a USB bus bottleneck.

    At most ``slots`` cameras are streaming at any instant.  The active
    window rotates every ``dwell`` seconds so that all cameras are eventually
    serviced.  Parked cameras keep showing their last captured frame.

    Usage::

        group = MultiplexGroup(cameras=[2, 4, 6], slots=2, frame_queue=q, ...)
        group.start()            # opens all devices, STREAMON first K
        while running:
            group.poll()         # grab frames from active cameras
            frames = group.get_all_frames()
        group.stop()             # close everything
    """

    def __init__(
        self,
        cameras: List[int],
        slots: int,
        frame_queue: queue.Queue,
        *,
        width: int = 640,
        height: int = 480,
        fps: int = 30,
        fourcc: str = "MJPG",
        dwell: float = 1.5,
        settle: float = 0.2,
        backend: str = "v4l2",
        hw_acceleration: bool = True,
    ) -> None:
        self.cameras = list(cameras)
        self.slots = min(slots, len(cameras))
        self.frame_queue = frame_queue
        self.width = width
        self.height = height
        self.fps = fps
        self.fourcc = fourcc
        self.dwell = dwell
        self.settle = settle
        self.backend = backend
        self.hw_acceleration = hw_acceleration

        self._active: OrderedDict[int, object] = OrderedDict()
        self._rr: deque[int] = deque(self.cameras)
        self._frames: Dict[int, Optional[np.ndarray]] = {c: None for c in cameras}
        self._last_fresh: Dict[int, Optional[float]] = {c: None for c in cameras}
        self._devices: Dict[int, object] = {}  # idx → V4L2Camera or VideoCapture
        self._last_rot: float = 0.0
        self._started = False
        self._lock = threading.Lock()

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Open all cameras, start streaming the first ``slots`` of them."""
        if self.backend == "v4l2" and sys.platform == "linux":
            self._start_v4l2()
        else:
            self._start_opencv()
        self._started = True
        self._last_rot = time.time()

    def stop(self) -> None:
        """Stop streaming and close all devices."""
        self._started = False
        if self.backend == "v4l2" and sys.platform == "linux":
            for dev in self._devices.values():
                if isinstance(dev, V4L2Camera):
                    dev.close()
        else:
            for dev in self._devices.values():
                if isinstance(dev, cv2.VideoCapture):
                    dev.release()
        self._devices.clear()
        self._active.clear()

    # -- V4L2 backend -------------------------------------------------------

    def _start_v4l2(self) -> None:
        """Open all cameras via raw V4L2 (fd + mmap but no streaming)."""
        for c in self.cameras:
            try:
                dev = V4L2Camera(c, self.width, self.height, self.fourcc)
                self._devices[c] = dev
            except OSError as e:
                logger.warning("cam%d: V4L2 open failed: %s", c, e)

        # Filter out failed cameras
        self.cameras = [c for c in self.cameras if c in self._devices]
        self._rr = deque(self.cameras)
        self._frames = {c: None for c in self.cameras}
        self._last_fresh = {c: None for c in self.cameras}

        # STREAMON the first `slots` cameras
        for _ in range(min(self.slots, len(self._rr))):
            idx = self._rr[0]
            self._rr.rotate(-1)
            dev = self._devices[idx]
            try:
                dev.start()
                dev.read(timeout=1.0)  # warm-up frame
                self._active[idx] = dev
                self._last_fresh[idx] = time.time()
                logger.info("cam%d: STREAMON (initial window)", idx)
            except OSError as e:
                logger.warning("cam%d: initial STREAMON failed: %s", idx, e)

    def _poll_v4l2(self) -> None:
        """Grab frames from active V4L2 cameras and rotate the window."""
        now = time.time()

        # select() over all live fds
        fd_map: Dict[int, int] = {}
        for idx, dev in self._active.items():
            if isinstance(dev, V4L2Camera) and dev.fd >= 0:
                fd_map[dev.fd] = idx

        if fd_map:
            ready, _, _ = select.select(list(fd_map), [], [], 0.2)
            for fd in ready:
                idx = fd_map[fd]
                dev = self._active[idx]
                fr = dev.grab()
                if fr is not None:
                    self._frames[idx] = fr
                    self._last_fresh[idx] = now
                    self.frame_queue.put((idx, fr))

    def _rotate_v4l2(self) -> None:
        """STREAMOFF the oldest active camera, STREAMON the next."""
        now = time.time()
        if len(self._devices) <= self.slots or now - self._last_rot < self.dwell:
            return

        # Rotate out the oldest
        victim, vdev = next(iter(self._active.items()))
        self._active.pop(victim)
        if isinstance(vdev, V4L2Camera):
            vdev.stop()
        logger.debug("cam%d: STREAMOFF (rotated out)", victim)

        if self.settle > 0:
            time.sleep(self.settle)

        # Find next candidate
        nxt = self._next_candidate()
        if nxt is not None:
            dev = self._devices[nxt]
            try:
                dev.start()
                dev.read(timeout=1.0)
                self._active[nxt] = dev
                self._last_fresh[nxt] = time.time()
                logger.debug("cam%d: STREAMON (rotated in)", nxt)
            except OSError as e:
                logger.warning("cam%d: STREAMON failed after rotation: %s", nxt, e)

        self._last_rot = time.time()

    # -- OpenCV backend ------------------------------------------------------

    def _start_opencv(self) -> None:
        """Open and stream the first `slots` cameras via OpenCV."""
        for _ in range(min(self.slots, len(self._rr))):
            idx = self._rr[0]
            self._rr.rotate(-1)
            cap = self._open_opencv_camera(idx)
            if cap is not None:
                self._devices[idx] = cap
                self._active[idx] = cap
                self._last_fresh[idx] = time.time()
                logger.info("cam%d: opened (initial window, opencv)", idx)
            else:
                logger.warning("cam%d: OpenCV open failed", idx)

        # Filter out cameras we couldn't open at all
        self.cameras = [c for c in self.cameras if c in self._devices]
        self._rr = deque(self.cameras)
        self._frames = {c: None for c in self.cameras}
        self._last_fresh = {c: None for c in self.cameras}

    def _open_opencv_camera(self, idx: int) -> Optional[cv2.VideoCapture]:
        """Open a camera with OpenCV and read one warm-up frame."""
        cap = cv2.VideoCapture(idx, cv2.CAP_V4L2)
        if not cap.isOpened():
            cap.release()
            return None
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*self.fourcc))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        cap.set(cv2.CAP_PROP_FPS, self.fps)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        ok, _ = cap.read()
        if not ok:
            cap.release()
            return None
        return cap

    def _poll_opencv(self) -> None:
        """Read frames from active OpenCV cameras."""
        now = time.time()
        for idx, cap in list(self._active.items()):
            if cap is None or not cap.isOpened():
                continue
            ok, fr = cap.read()
            if ok and fr is not None:
                self._frames[idx] = fr
                self._last_fresh[idx] = now
                self.frame_queue.put((idx, fr))

    def _rotate_opencv(self) -> None:
        """Release the oldest camera, open the next."""
        now = time.time()
        if len(self._devices) <= self.slots or now - self._last_rot < self.dwell:
            return

        # Release oldest
        victim, vcap = next(iter(self._active.items()))
        self._active.pop(victim)
        if isinstance(vcap, cv2.VideoCapture):
            vcap.release()
        # Remove from devices so it can be re-opened fresh
        self._devices.pop(victim, None)
        logger.debug("cam%d: released (rotated out, opencv)", victim)

        if self.settle > 0:
            time.sleep(self.settle)

        nxt = self._next_candidate()
        if nxt is not None:
            cap = self._open_opencv_camera(nxt)
            if cap is not None:
                self._devices[nxt] = cap
                self._active[nxt] = cap
                self._last_fresh[nxt] = time.time()
                logger.debug("cam%d: opened (rotated in, opencv)", nxt)
            else:
                logger.warning("cam%d: OpenCV open failed after rotation", nxt)

        self._last_rot = time.time()

    # -- common helpers ------------------------------------------------------

    def _next_candidate(self) -> Optional[int]:
        """Find the next camera in round-robin order that is not active."""
        for _ in range(len(self._rr)):
            cand = self._rr[0]
            self._rr.rotate(-1)
            if cand not in self._active:
                return cand
        return None

    def poll(self) -> None:
        """Grab frames and maybe rotate the window. Call this in the main loop."""
        if not self._started:
            return

        if self.backend == "v4l2" and sys.platform == "linux":
            self._poll_v4l2()
            self._rotate_v4l2()
        else:
            self._poll_opencv()
            self._rotate_opencv()

    def get_all_frames(self) -> Dict[int, Optional[np.ndarray]]:
        """Return the current frame dict (live or parked) for all cameras."""
        return dict(self._frames)

    def get_active_cameras(self) -> Set[int]:
        """Return the set of currently streaming camera indices."""
        with self._lock:
            return set(self._active.keys())

    def get_last_fresh(self) -> Dict[int, Optional[float]]:
        """Return the last-fresh timestamp for each camera."""
        return dict(self._last_fresh)


# ---------------------------------------------------------------------------
# MultiplexScheduler — orchestrates groups and passes through free cameras
# ---------------------------------------------------------------------------

class MultiplexScheduler:
    """Manages multiplex groups for hub-contended cameras.

    Cameras that *don't* need multiplexing are not managed by the scheduler
    — they run through the normal ``USBCameraThread`` path.  The scheduler
    only handles cameras in groups where N > K.

    Usage::

        scheduler = MultiplexScheduler()
        scheduler.configure(camera_indices, mode="auto", ...)
        scheduler.start()
        while running:
            scheduler.poll()
        scheduler.stop()
    """

    def __init__(
        self,
        frame_queue: queue.Queue,
        *,
        width: int = 640,
        height: int = 480,
        fps: int = 30,
        fourcc: str = "MJPG",
        hw_acceleration: bool = True,
    ) -> None:
        self.frame_queue = frame_queue
        self.width = width
        self.height = height
        self.fps = fps
        self.fourcc = fourcc
        self.hw_acceleration = hw_acceleration

        self._groups: Dict[str, MultiplexGroup] = {}
        self._multiplex_cameras: List[int] = []
        self._camera_group: Dict[int, str] = {}
        self._group_slots: Dict[str, int] = {}
        self._started = False

        # Configurable multiplex params (set via configure())
        self._mode: str = "auto"
        self._slots: int = 2
        self._dwell: float = 1.5
        self._settle: float = 0.2
        self._backend: str = "v4l2"

    def configure(
        self,
        camera_indices: List[int],
        *,
        mode: str = "auto",
        slots: int = 2,
        dwell: float = 1.5,
        settle: float = 0.2,
        backend: str = "v4l2",
    ) -> List[int]:
        """Determine which cameras need multiplexing and build groups.

        Args:
            camera_indices: all available camera indices
            mode: "auto", "off", or "force"
            slots: max simultaneous streams (K)
            dwell: seconds a camera stays live before rotating out
            settle: pause after releasing a camera before opening next
            backend: "v4l2" or "opencv"

        Returns:
            List of camera indices that need multiplexing (the caller
            should NOT start USBCameraThread for these).
        """
        self._mode = mode
        self._slots = slots
        self._dwell = dwell
        self._settle = settle
        self._backend = backend

        self._camera_group, self._group_slots, self._multiplex_cameras = (
            needs_multiplexing(camera_indices, mode=mode, default_slots=slots)
        )

        # Build one MultiplexGroup per group that has contention
        self._groups.clear()
        from collections import defaultdict
        group_cams: Dict[str, List[int]] = defaultdict(list)
        for idx in self._multiplex_cameras:
            gid = self._camera_group[idx]
            group_cams[gid].append(idx)

        for gid, cams in group_cams.items():
            k = self._group_slots[gid]
            self._groups[gid] = MultiplexGroup(
                cameras=cams,
                slots=k,
                frame_queue=self.frame_queue,
                width=self.width,
                height=self.height,
                fps=self.fps,
                fourcc=self.fourcc,
                dwell=self._dwell,
                settle=self._settle,
                backend=self._backend,
                hw_acceleration=self.hw_acceleration,
            )

        if self._multiplex_cameras:
            logger.info(
                "Multiplex: %d cameras in %d group(s), mode=%s, backend=%s",
                len(self._multiplex_cameras), len(self._groups), mode, backend,
            )
        else:
            logger.info("Multiplex: no contention detected — all cameras unrestricted")

        return self._multiplex_cameras

    def start(self) -> None:
        """Start all multiplex groups."""
        for gid, group in self._groups.items():
            logger.info("Starting multiplex group %s: %s", gid, group.cameras)
            group.start()
        self._started = True

    def stop(self) -> None:
        """Stop all multiplex groups."""
        self._started = False
        for group in self._groups.values():
            group.stop()

    def poll(self) -> None:
        """Poll all groups (grab frames + maybe rotate). Call in main loop."""
        if not self._started:
            return
        for group in self._groups.values():
            group.poll()

    def get_multiplex_cameras(self) -> List[int]:
        """Return the list of camera indices managed by the scheduler."""
        return list(self._multiplex_cameras)

    def get_active_cameras(self) -> Set[int]:
        """Return the set of currently-streaming camera indices across all groups."""
        active: Set[int] = set()
        for group in self._groups.values():
            active.update(group.get_active_cameras())
        return active

    def get_all_frames(self) -> Dict[int, Optional[np.ndarray]]:
        """Return frames from all multiplexed cameras (live + parked)."""
        frames: Dict[int, Optional[np.ndarray]] = {}
        for group in self._groups.values():
            frames.update(group.get_all_frames())
        return frames

    def get_last_fresh(self) -> Dict[int, Optional[float]]:
        """Return last-fresh timestamps for all multiplexed cameras."""
        result: Dict[int, Optional[float]] = {}
        for group in self._groups.values():
            result.update(group.get_last_fresh())
        return result
