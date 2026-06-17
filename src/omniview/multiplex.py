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
            try:
                ready, _, _ = select.select(list(fd_map), [], [], 0.2)
            except (OSError, ValueError):
                # Bad file descriptor — one or more cameras were unplugged.
                # Fall through; individual grab() calls will detect the dead
                # devices and trigger removal below.
                ready = []
            dead: List[int] = []
            for fd in ready:
                idx = fd_map.get(fd)
                if idx is None:
                    continue
                dev = self._active.get(idx)
                if dev is None:
                    continue
                try:
                    fr = dev.grab()
                except OSError:
                    fr = None
                    dead.append(idx)
                if fr is not None:
                    self._frames[idx] = fr
                    self._last_fresh[idx] = now
                    self.frame_queue.put((idx, fr))
            for idx in dead:
                logger.warning("cam%d: grab failed (disconnected?)", idx)
                self.remove_camera(idx)

    def _rotate_v4l2(self) -> None:
        """STREAMOFF the oldest active camera, STREAMON the next."""
        now = time.time()
        if len(self._devices) <= self.slots or now - self._last_rot < self.dwell:
            return

        # Rotate out the oldest
        victim, vdev = next(iter(self._active.items()))
        self._active.pop(victim)
        if isinstance(vdev, V4L2Camera):
            try:
                vdev.stop()
            except OSError:
                logger.debug("cam%d: STREAMOFF failed (already gone)", victim)
        logger.debug("cam%d: STREAMOFF (rotated out)", victim)

        if self.settle > 0:
            time.sleep(self.settle)

        # Find next candidate
        nxt = self._next_candidate()
        if nxt is not None:
            dev = self._devices.get(nxt)
            if dev is not None:
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
        dead: List[int] = []
        for idx, cap in list(self._active.items()):
            if cap is None or not cap.isOpened():
                dead.append(idx)
                continue
            try:
                ok, fr = cap.read()
            except Exception:
                ok = False
                dead.append(idx)
            if ok and fr is not None:
                self._frames[idx] = fr
                self._last_fresh[idx] = now
                self.frame_queue.put((idx, fr))
        for idx in dead:
            logger.warning("cam%d: read failed (disconnected?)", idx)
            self.remove_camera(idx)

    def _rotate_opencv(self) -> None:
        """Release the oldest camera, open the next."""
        now = time.time()
        if len(self._devices) <= self.slots or now - self._last_rot < self.dwell:
            return

        # Release oldest
        victim, vcap = next(iter(self._active.items()))
        self._active.pop(victim)
        if isinstance(vcap, cv2.VideoCapture):
            try:
                vcap.release()
            except Exception:
                logger.debug("cam%d: release failed on rotation (already gone)", victim)
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

        try:
            if self.backend == "v4l2" and sys.platform == "linux":
                self._poll_v4l2()
                self._rotate_v4l2()
            else:
                self._poll_opencv()
                self._rotate_opencv()
        except Exception:
            # Catch-all: a hot-unplug can cause unexpected errors (EBADF,
            # ENODEV, etc.) at any point.  Swallow so the monitor thread
            # stays alive — the next _prune pass will clean up.
            logger.debug("poll() error (will retry)", exc_info=True)

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

    # -- hot-unplug handling -------------------------------------------------

    def remove_camera(self, idx: int) -> None:
        """Tear down and forget a single camera (e.g. it was unplugged).

        Releases the device, drops it from the active window, the round-robin
        order and the frame / timestamp maps.  If the removed camera was
        streaming, a parked survivor is promoted into the freed slot so it
        does not stay frozen on its last frame.
        """
        if idx not in self._frames and idx not in self._devices:
            return

        with self._lock:
            was_active = idx in self._active
            self._active.pop(idx, None)
            dev = self._devices.pop(idx, None)

        # Release the device outside the lock (close()/release() may block).
        if dev is not None:
            try:
                if isinstance(dev, V4L2Camera):
                    dev.close()
                elif isinstance(dev, cv2.VideoCapture):
                    dev.release()
            except Exception as e:  # pragma: no cover - best-effort teardown
                logger.debug("cam%d: release on removal failed: %s", idx, e)

        with self._lock:
            self._frames.pop(idx, None)
            self._last_fresh.pop(idx, None)
            if idx in self.cameras:
                self.cameras.remove(idx)
            self._rr = deque(c for c in self._rr if c != idx)

        logger.info("cam%d: removed from multiplex group (disconnected)", idx)

        # Re-fill the window if removing an active camera left a slot open.
        if was_active and self._started:
            self._fill_active_slots()

    def _fill_active_slots(self) -> None:
        """STREAMON / open parked cameras until the active window is full.

        Called after a camera is removed so a freed slot is taken by a parked
        survivor.  Without this, a surviving camera could stay parked (frozen
        on its last frame) forever because rotation is skipped once the
        device count drops to ``slots``.
        """
        target = min(self.slots, len(self.cameras))
        use_v4l2 = self.backend == "v4l2" and sys.platform == "linux"
        while len(self._active) < target:
            nxt = self._next_candidate()
            if nxt is None:
                break
            if use_v4l2:
                dev = self._devices.get(nxt)
                if dev is None:
                    break
                try:
                    dev.start()
                    dev.read(timeout=1.0)
                    self._active[nxt] = dev
                    self._last_fresh[nxt] = time.time()
                    logger.debug("cam%d: STREAMON (slot refill)", nxt)
                except OSError as e:
                    logger.warning("cam%d: STREAMON failed on refill: %s", nxt, e)
                    break
            else:
                cap = self._open_opencv_camera(nxt)
                if cap is None:
                    logger.warning("cam%d: OpenCV open failed on refill", nxt)
                    break
                self._devices[nxt] = cap
                self._active[nxt] = cap
                self._last_fresh[nxt] = time.time()
                logger.debug("cam%d: opened (slot refill, opencv)", nxt)

    def add_cameras(self, cameras: List[int]) -> None:
        """Add new cameras to an already-running group (hot-plug).

        Opens the new devices and places them in the round-robin.  If the
        active window has spare slots, the new cameras are STREAMON'd / opened
        immediately; otherwise they are parked and will be rotated in.
        """
        use_v4l2 = self.backend == "v4l2" and sys.platform == "linux"
        for c in cameras:
            if c in self._devices or c in self._frames:
                continue  # already known

            if use_v4l2:
                try:
                    dev = V4L2Camera(c, self.width, self.height, self.fourcc)
                    self._devices[c] = dev
                except OSError as e:
                    logger.warning("cam%d: V4L2 open failed on add: %s", c, e)
                    continue
            # OpenCV devices are opened lazily on STREAMON / rotation.

            self.cameras.append(c)
            self._rr.append(c)
            self._frames[c] = None
            self._last_fresh[c] = None
            logger.info("cam%d: added to multiplex group (hot-plug)", c)

        # Recalculate slots (may have grown)
        self.slots = min(self.slots, len(self.cameras))

        # Fill any spare active slots with the newly added cameras
        if self._started:
            self._fill_active_slots()


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

    def reconfigure(self, camera_indices: List[int]) -> Tuple[Set[int], Set[int]]:
        """Re-evaluate multiplex topology after a hot-plug event.

        Re-runs ``needs_multiplexing`` with the current device list and
        adjusts groups accordingly: new congested cameras are added to the
        appropriate group (or a new group is created), cameras that are no
        longer congested (or no longer present) are removed.

        Args:
            camera_indices: the full set of currently available camera
                indices (same as what was passed to ``configure``).

        Returns:
            ``(added, removed)`` where *added* are camera indices newly
            placed under multiplex management (the caller should stop
            their per-camera threads) and *removed* are cameras that left
            multiplex management (the caller may start threads for them).
        """
        new_cg, new_gs, new_mc = needs_multiplexing(
            camera_indices, mode=self._mode, default_slots=self._slots
        )

        old_mpx = set(self._multiplex_cameras)
        new_mpx = set(new_mc)
        added: Set[int] = new_mpx - old_mpx
        removed: Set[int] = old_mpx - new_mpx

        if not added and not removed:
            return added, removed

        # --- Remove cameras that are no longer multiplexed ---
        for idx in removed:
            gid = self._camera_group.get(idx)
            group = self._groups.get(gid) if gid is not None else None
            if group is not None:
                group.remove_camera(idx)
                if not group.cameras:
                    group.stop()
                    self._groups.pop(gid, None)
            self._camera_group.pop(idx, None)

        # --- Add cameras that are newly multiplexed ---
        from collections import defaultdict
        new_group_cams: Dict[str, List[int]] = defaultdict(list)
        for idx in added:
            gid = new_cg[idx]
            new_group_cams[gid].append(idx)

        for gid, cams in new_group_cams.items():
            k = new_gs[gid]
            existing = self._groups.get(gid)
            if existing is not None:
                # Add cameras to the existing group
                existing.add_cameras(cams)
            else:
                # Create a new group
                group = MultiplexGroup(
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
                group.start()
                self._groups[gid] = group

        # Update bookkeeping
        self._camera_group = new_cg
        self._group_slots = new_gs
        self._multiplex_cameras = new_mc

        if added:
            logger.info("Multiplex reconfigure: added cameras %s", sorted(added))
        if removed:
            logger.info("Multiplex reconfigure: removed cameras %s", sorted(removed))

        return added, removed

    def get_multiplex_cameras(self) -> List[int]:
        """Return the list of camera indices managed by the scheduler."""
        return list(self._multiplex_cameras)

    def sync_available(self, present: Set[int]) -> Set[int]:
        """Prune multiplexed cameras whose device nodes have disappeared.

        Args:
            present: indices that currently exist (e.g. from
                :func:`omniview.usb_topology.present_video_devices`).

        Any managed camera absent from *present* is removed from its group
        (closing the device).  A group left with no cameras is stopped and
        discarded.  This is what makes multiplexed cameras disappear after a
        hub is unplugged, mirroring the hot-unplug removal that ordinary
        per-thread cameras already get.

        Returns:
            The set of camera indices that were removed (empty if nothing
            changed).
        """
        missing = set(self._multiplex_cameras) - set(present)
        if not missing:
            return set()

        for idx in missing:
            gid = self._camera_group.get(idx)
            group = self._groups.get(gid) if gid is not None else None
            if group is not None:
                group.remove_camera(idx)
                if not group.cameras:
                    group.stop()
                    self._groups.pop(gid, None)
                    self._group_slots.pop(gid, None)
            self._camera_group.pop(idx, None)

        self._multiplex_cameras = [
            c for c in self._multiplex_cameras if c not in missing
        ]
        logger.info("Multiplex: dropped disconnected cameras %s", sorted(missing))
        return missing

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
