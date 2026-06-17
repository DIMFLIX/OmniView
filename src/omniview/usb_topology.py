"""USB topology discovery — determine which V4L2 cameras share a bus.

The core problem: a USB 2.0 hub can only schedule a limited number of
simultaneous isochronous UVC streams (empirically K=2).  Opening more than K
cameras on the same bus fails with ENOSPC.

This module inspects ``/sys/class/video4linux`` symlinks to map each
``/dev/videoN`` device to its USB parent, then groups cameras that share a
common hub ancestor.  Cameras connected directly to a root hub (no
intermediate hub) are placed in singleton groups — they need no multiplexing.

Example sysfs paths on a typical machine::

    video0 → …/usb1/1-3/1-3:1.0/…      (root port, no hub)
    video2 → …/usb3/3-1/3-1.4/3-1.4.4/3-1.4.4:1.0/…  (behind hub 3-1)
    video4 → …/usb3/3-1/3-1.1/3-1.1.4/3-1.1.4.4/…    (behind hub 3-1)

All cameras whose paths share ``3-1`` as the first non-root segment are in
the same group.

Public API
----------
:func:`probe_usb_bus_groups` — the main entry point.
"""

from __future__ import annotations

import ctypes
import fcntl
import logging
import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# --- V4L2 capability query (VIDIOC_QUERYCAP) -------------------------------
# Modern UVC cameras expose extra /dev/videoN "metadata" nodes next to their
# real capture node (e.g. video0 = capture, video1 = metadata).  Those nodes
# cannot be opened for video capture, so treating them as cameras makes the
# manager spawn doomed threads (endless "can't open camera by index" / EINVAL
# failures + restart loops) and miscount cameras when grouping USB topology.
# VIDIOC_QUERYCAP lets us cheaply tell a capture node from a metadata node
# without starting a stream.
_V4L2_CAP_VIDEO_CAPTURE = 0x00000001
_V4L2_CAP_DEVICE_CAPS = 0x80000000


class _v4l2_capability(ctypes.Structure):
    """struct v4l2_capability from <linux/videodev2.h>."""

    _fields_ = [
        ("driver", ctypes.c_char * 16),
        ("card", ctypes.c_char * 32),
        ("bus_info", ctypes.c_char * 32),
        ("version", ctypes.c_uint32),
        ("capabilities", ctypes.c_uint32),
        ("device_caps", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32 * 3),
    ]


def _vidioc_querycap() -> int:
    """Compute the VIDIOC_QUERYCAP ioctl request number (_IOR('V', 0, ...))."""
    size = ctypes.sizeof(_v4l2_capability)
    op = (2 << 30) | (size << 16) | (ord("V") << 8) | 0  # dir=_IOC_READ
    # fcntl.ioctl expects a signed C int; fold values >= 0x80000000.
    return op - 0x100000000 if op >= 0x80000000 else op


_VIDIOC_QUERYCAP = _vidioc_querycap()


def _supports_video_capture(idx: int) -> Optional[bool]:
    """Return whether ``/dev/videoN`` supports video capture.

    Returns ``True``/``False`` from the node's V4L2 capabilities, or ``None``
    when it can't be determined (open/ioctl failure) so callers can decide to
    keep the device rather than hide a real camera on a transient glitch.
    """
    dev = f"/dev/video{idx}"
    try:
        fd = os.open(dev, os.O_RDWR | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        cap = _v4l2_capability()
        fcntl.ioctl(fd, _VIDIOC_QUERYCAP, cap)
    except OSError:
        return None
    finally:
        os.close(fd)
    # device_caps describes THIS node; capabilities is the union across all
    # nodes of the physical device (so a metadata node would wrongly look
    # capture-capable if we used it).  Prefer device_caps when available.
    caps = (
        cap.device_caps
        if cap.capabilities & _V4L2_CAP_DEVICE_CAPS
        else cap.capabilities
    )
    return bool(caps & _V4L2_CAP_VIDEO_CAPTURE)


# Pattern: a USB device-port segment like "3-1", "1-3.4", "3-1.1.4".
# The first group before the dot is the hub-level port under the root hub.
_USB_SEGMENT_RE = re.compile(r"^\d+-\d+(?:\.\d+)*$")


def _video_sysfs_paths() -> Dict[int, str]:
    """Return {video_index: resolved_sysfs_path} for all /dev/videoN devices."""
    v4l_class = Path("/sys/class/video4linux")
    result: Dict[int, str] = {}
    if not v4l_class.is_dir():
        logger.warning("/sys/class/video4linux not found — USB topology unavailable")
        return result
    for entry in sorted(v4l_class.iterdir()):
        name = entry.name
        if not name.startswith("video"):
            continue
        try:
            idx = int(name.removeprefix("video"))
        except ValueError:
            continue
        resolved = str(entry.resolve())
        result[idx] = resolved
    return result


def present_video_devices() -> Optional[Set[int]]:
    """Return the set of ``/dev/videoN`` indices currently present in sysfs.

    Reads ``/sys/class/video4linux`` (the same source as the topology probe)
    to learn which video device nodes physically exist *right now*.  This is
    a cheap, non-intrusive presence check: unlike opening the device with
    OpenCV, it does not contend with the multiplex scheduler over the busy
    device nodes it is actively streaming, so it stays reliable even while
    cameras are open.

    Returns:
        The set of present video indices, or ``None`` when sysfs is
        unavailable (e.g. non-Linux platforms), signalling callers that
        sysfs-based presence detection cannot be used.
    """
    v4l_class = Path("/sys/class/video4linux")
    if not v4l_class.is_dir():
        return None
    present: Set[int] = set()
    for entry in v4l_class.iterdir():
        name = entry.name
        if not name.startswith("video"):
            continue
        try:
            present.add(int(name.removeprefix("video")))
        except ValueError:
            continue
    return present


def present_capture_devices() -> Optional[Set[int]]:
    """Like :func:`present_video_devices`, but only capture-capable nodes.

    Filters the sysfs presence set through :func:`_supports_video_capture`
    so the metadata-only ``/dev/videoN`` nodes that modern UVC cameras expose
    are excluded.  Including them makes the manager spawn doomed camera
    threads (endless open failures + restart loops) and miscount cameras when
    grouping USB topology.

    Nodes whose capability can't be determined are kept (fail open) so a real
    camera is never hidden by a transient probe failure.

    Returns:
        The set of capture-capable video indices, or ``None`` when sysfs is
        unavailable (e.g. non-Linux platforms), mirroring
        :func:`present_video_devices`.
    """
    present = present_video_devices()
    if present is None:
        return None
    capture: Set[int] = set()
    for idx in present:
        supported = _supports_video_capture(idx)
        if supported is None or supported:
            capture.add(idx)
    return capture


def _extract_usb_parent(path: str) -> Tuple[str, int]:
    """From a resolved sysfs path, extract the shared hub ancestor and depth.

    Returns (group_key, depth) where:
    - group_key: the USB path segment representing the shared hub
      (e.g. "3-1" for devices under that hub)
    - depth: number of USB device-port segments in the full path
      (depth=1 means directly connected to root hub → no multiplexing)

    The grouping heuristic: find the first non-root USB device-port segment
    after the root hub (e.g. "usb3") — that segment identifies the shared
    hub.  All cameras sharing that segment are in the same group.

    For cameras directly on a root hub port (e.g. ``usb1/1-3``), the
    *device-level* segment (the part before the colon in an interface
    segment like ``1-3:1.0``) is used as a group key.  This means that
    multi-interface V4L2 entries (e.g. ``/dev/video0`` and ``/dev/video1``
    from the same UVC camera) are correctly grouped together as a single
    camera — they share the same USB endpoint and only one can stream at
    a time.
    """
    # Split path into parts and find USB segments
    # Path looks like: …/pci…/usb3/3-1/3-1.4/3-1.4.4/3-1.4.4:1.0/video4linux/video2
    parts = path.split("/")
    usb_segments: List[str] = []
    in_usb_tree = False

    for part in parts:
        if part.startswith("usb") and part[3:].isdigit():
            in_usb_tree = True
            continue
        if in_usb_tree and _USB_SEGMENT_RE.match(part):
            usb_segments.append(part)
        elif in_usb_tree and ":" in part:
            # Interface segment (e.g. "3-1.4.4:1.0").  Extract the device
            # portion (before the colon) so that multi-interface entries
            # for the same physical camera share a group key.
            device_part = part.split(":")[0]
            if _USB_SEGMENT_RE.match(device_part) and device_part not in usb_segments:
                usb_segments.append(device_part)
            break

    if not usb_segments:
        # No USB segments found — return unique group
        return path, 0

    depth = len(usb_segments)

    if depth == 1:
        # Directly on root hub port → use the device segment as key.
        # Multi-interface cameras (e.g. 1-3:1.0 and 1-3:1.1) will share
        # the same key ("1-3") since we extracted the pre-colon part.
        return usb_segments[0], 1

    # depth >= 2: there's at least one hub between root and device.
    # The first segment (e.g. "3-1") is the shared hub ancestor.
    # All cameras with the same first segment share the same bus bottleneck.
    return usb_segments[0], depth


def _detect_usb_speed(group_key: str) -> float:
    """Estimate the USB speed for a group (used to determine max_slots).

    Checks ``/sys/bus/usb/devices/<group_key>/speed`` if available.
    Returns the speed in Mbps, or 480.0 (USB 2.0 default) as fallback.
    """
    try:
        speed_path = Path(f"/sys/bus/usb/devices/{group_key}/speed")
        if speed_path.exists():
            return float(speed_path.read_text().strip())
    except (OSError, ValueError):
        pass
    return 480.0  # assume USB 2.0


def _usb_speed_to_slots(speed_mbps: float) -> int:
    """Map USB bus speed to the max simultaneous isochronous streams (K).

    USB 3.0+ (5000+ Mbps): no practical isochronous limit → 0 (unlimited).
    USB 2.0 (480 Mbps): empirically K=2 on OmniView test rig.
    USB 1.x (12 Mbps): K=1 (barely enough for one stream).
    """
    if speed_mbps >= 5000:
        return 0  # unlimited — no multiplexing needed
    if speed_mbps >= 480:
        return 2
    return 1


def probe_usb_bus_groups(
    camera_indices: List[int],
    default_slots: int = 2,
) -> Tuple[Dict[int, str], Dict[str, int]]:
    """Probe USB topology and group cameras by shared bus bottleneck.

    Args:
        camera_indices: list of /dev/videoN indices to classify
        default_slots: fallback K (max simultaneous streams) when USB
            speed cannot be determined (default 2)

    Returns:
        A tuple of two dicts:

        1. ``camera_group``: ``{camera_index: group_id}`` — each camera is
           assigned to a group.  Cameras sharing a hub get the same group_id.
           Cameras directly on root hub ports get unique groups.

        2. ``group_slots``: ``{group_id: max_slots}`` — the maximum number
           of simultaneous streams the bus can support for that group.
           A value of 0 means "unlimited" (USB 3.0+), which signals the
           caller that no multiplexing is needed.
    """
    sysfs_map = _video_sysfs_paths()

    camera_group: Dict[int, str] = {}
    group_slots: Dict[str, int] = {}

    for idx in camera_indices:
        path = sysfs_map.get(idx)
        if path is None:
            # No sysfs info — assign a unique group so it's treated as
            # unrestricted (no multiplexing)
            group_key = f"_unknown_{idx}"
            camera_group[idx] = group_key
            group_slots[group_key] = 0
            logger.debug("camera %d: no sysfs path → unique group (unlimited)", idx)
            continue

        hub_key, depth = _extract_usb_parent(path)
        camera_group[idx] = hub_key

        if hub_key not in group_slots:
            if depth <= 1:
                # Directly on root hub → no multiplexing needed
                group_slots[hub_key] = 0
                logger.debug("camera %d: direct root-hub connection (no multiplex)", idx)
            else:
                speed = _detect_usb_speed(hub_key)
                slots = _usb_speed_to_slots(speed) or default_slots
                group_slots[hub_key] = slots
                logger.debug(
                    "camera %d: hub %s (speed=%.0f Mbps, slots=%d)",
                    idx, hub_key, speed, slots,
                )

    return camera_group, group_slots


def needs_multiplexing(
    camera_indices: List[int],
    mode: str = "auto",
    default_slots: int = 2,
) -> Tuple[Dict[int, str], Dict[str, int], List[int]]:
    """Determine which cameras need multiplexing.

    Args:
        camera_indices: list of camera indices
        mode: one of:
            - ``"auto"``: detect from USB topology (default)
            - ``"off"``: never multiplex
            - ``"force"``: multiplex all cameras as if they share one hub
        default_slots: K when auto-detecting

    Returns:
        (camera_group, group_slots, multiplex_cameras) where
        *multiplex_cameras* is the list of camera indices that need
        time-multiplexing (groups where N > K and K > 0).
    """
    if mode == "off":
        group_key = "_no_multiplex"
        camera_group = {idx: group_key for idx in camera_indices}
        group_slots = {group_key: 0}
        return camera_group, group_slots, []

    if mode == "force":
        group_key = "_forced"
        camera_group = {idx: group_key for idx in camera_indices}
        group_slots = {group_key: default_slots}
        n = len(camera_indices)
        multiplex_cameras = camera_indices if n > default_slots and default_slots > 0 else []
        return camera_group, group_slots, multiplex_cameras

    # mode == "auto"
    camera_group, group_slots = probe_usb_bus_groups(camera_indices, default_slots)

    # Find cameras in groups where N > K (and K > 0, i.e. not unlimited)
    from collections import Counter
    group_counts = Counter(camera_group.values())
    multiplex_cameras: List[int] = []
    for idx in camera_indices:
        gid = camera_group[idx]
        k = group_slots[gid]
        if k > 0 and group_counts[gid] > k:
            multiplex_cameras.append(idx)

    return camera_group, group_slots, multiplex_cameras
