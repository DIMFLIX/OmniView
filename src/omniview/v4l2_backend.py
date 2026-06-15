"""Low-level V4L2 camera backend for fast bus-slot rotation.

Why this exists
---------------
OpenCV's ``VideoCapture`` has no "pause": to free a camera's USB isochronous
slot you must call ``cap.release()``, which fully closes the device (fd,
UVC streaming interface, buffers). Re-opening costs ~560 ms because the
whole probe/negotiate/allocate path runs again.

The USB isochronous bandwidth is reserved on ``VIDIOC_STREAMON`` and released
on ``VIDIOC_STREAMOFF`` — and importantly, **STREAMOFF does NOT require
closing the fd or unmapping buffers**.  By driving V4L2 directly we can keep
all N camera fds open with buffers mmap'd, and merely toggle STREAMON /
STREAMOFF to move K "live slots" between cameras.

Ported from ``examples/multiplex_rotation_demo.py`` into the library.
"""

from __future__ import annotations

import ctypes
import errno
import fcntl
import logging
import mmap
import os
import select
import time
from typing import List, Optional

import cv2
import numpy as np

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# V4L2 struct layouts (from /usr/include/linux/videodev2.h)
# ---------------------------------------------------------------------------

u8 = ctypes.c_uint8
u32 = ctypes.c_uint32
i32 = ctypes.c_int32

V4L2_BUF_TYPE_VIDEO_CAPTURE = 1
V4L2_MEMORY_MMAP = 1
V4L2_FIELD_NONE = 1


def _fourcc(code: str) -> int:
    """Pack a 4-char code (e.g. 'MJPG') into a V4L2 pixelformat u32."""
    code = (code + "    ")[:4]
    return (
        ord(code[0])
        | (ord(code[1]) << 8)
        | (ord(code[2]) << 16)
        | (ord(code[3]) << 24)
    )


FOURCC_MJPG = _fourcc("MJPG")
FOURCC_YUYV = _fourcc("YUYV")


class v4l2_pix_format(ctypes.Structure):
    _fields_ = [
        ("width", u32), ("height", u32), ("pixelformat", u32),
        ("field", u32), ("bytesperline", u32), ("sizeimage", u32),
        ("colorspace", u32), ("priv", u32), ("flags", u32),
        ("enc", u32), ("quantization", u32), ("xfer_func", u32),
    ]


class v4l2_format(ctypes.Structure):
    _fields_ = [
        ("type", u32),
        ("_pad", u32),
        ("pix", v4l2_pix_format),
        ("_fill", u8 * (200 - ctypes.sizeof(v4l2_pix_format))),
    ]


class v4l2_requestbuffers(ctypes.Structure):
    _fields_ = [
        ("count", u32), ("type", u32), ("memory", u32),
        ("capabilities", u32), ("flags", u8), ("reserved", u8 * 3),
    ]


class _timeval(ctypes.Structure):
    _fields_ = [("tv_sec", ctypes.c_long), ("tv_usec", ctypes.c_long)]


class v4l2_timecode(ctypes.Structure):
    _fields_ = [
        ("type", u32), ("flags", u32),
        ("frames", u8), ("seconds", u8), ("minutes", u8), ("hours", u8),
        ("userbits", u8 * 4),
    ]


class _buf_m(ctypes.Union):
    _fields_ = [
        ("offset", u32), ("userptr", ctypes.c_ulong),
        ("planes", ctypes.c_void_p), ("fd", i32),
    ]


class _buf_u2(ctypes.Union):
    _fields_ = [("request_fd", i32), ("reserved", u32)]


class v4l2_buffer(ctypes.Structure):
    _fields_ = [
        ("index", u32), ("type", u32), ("bytesused", u32), ("flags", u32),
        ("field", u32), ("timestamp", _timeval), ("timecode", v4l2_timecode),
        ("sequence", u32), ("memory", u32), ("m", _buf_m),
        ("length", u32), ("reserved2", u32), ("u2", _buf_u2),
    ]


# ABI assertions — fail loudly on 32-bit or mismatched headers.
assert ctypes.sizeof(v4l2_pix_format) == 48
assert ctypes.sizeof(v4l2_format) == 208
assert ctypes.sizeof(v4l2_requestbuffers) == 20
assert ctypes.sizeof(v4l2_buffer) == 88

# ---------------------------------------------------------------------------
# ioctl helpers
# ---------------------------------------------------------------------------

_IOC_TYPESHIFT = 8
_IOC_SIZESHIFT = 16
_IOC_DIRSHIFT = 30
_IOC_WRITE, _IOC_READ = 1, 2


def _IOC(direction, type_char, nr, size):
    op = (
        (direction << _IOC_DIRSHIFT)
        | (ord(type_char) << _IOC_TYPESHIFT)
        | (nr << 0)
        | (size << _IOC_SIZESHIFT)
    )
    return op - 0x100000000 if op >= 0x80000000 else op


def _IOW(t, nr, sz):
    return _IOC(_IOC_WRITE, t, nr, sz)


def _IOWR(t, nr, sz):
    return _IOC(_IOC_READ | _IOC_WRITE, t, nr, sz)


VIDIOC_S_FMT = _IOWR("V", 5, ctypes.sizeof(v4l2_format))
VIDIOC_REQBUFS = _IOWR("V", 8, ctypes.sizeof(v4l2_requestbuffers))
VIDIOC_QUERYBUF = _IOWR("V", 9, ctypes.sizeof(v4l2_buffer))
VIDIOC_QBUF = _IOWR("V", 15, ctypes.sizeof(v4l2_buffer))
VIDIOC_DQBUF = _IOWR("V", 17, ctypes.sizeof(v4l2_buffer))
VIDIOC_STREAMON = _IOW("V", 18, ctypes.sizeof(ctypes.c_int))
VIDIOC_STREAMOFF = _IOW("V", 19, ctypes.sizeof(ctypes.c_int))


def _xioctl(fd, request, arg):
    """ioctl wrapper that retries on EINTR."""
    while True:
        try:
            return fcntl.ioctl(fd, request, arg)
        except OSError as e:
            if e.errno == errno.EINTR:
                continue
            raise


# ---------------------------------------------------------------------------
# V4L2Camera
# ---------------------------------------------------------------------------

class V4L2Camera:
    """A single camera driven directly through V4L2.

    ``start()`` / ``stop()`` map to ``VIDIOC_STREAMON`` / ``STREAMOFF``,
    which acquire/release the USB isochronous slot *without* closing the
    device.  Opening the fd + allocating/mmapping buffers (done in
    ``__init__``) does NOT touch the bus schedule, so all N cameras can be
    constructed up front and only ``slots`` of them STREAMON'd at any instant.
    """

    def __init__(
        self,
        idx: int,
        width: int = 640,
        height: int = 480,
        fourcc: str = "MJPG",
        nbuffers: int = 4,
    ) -> None:
        self.idx = idx
        self.width = width
        self.height = height
        self.pixelformat: int = 0
        self.fd: int = -1
        self.nbuffers = nbuffers
        self.buffers: List[mmap.mmap] = []
        self.streaming = False

        # O_NONBLOCK so DQBUF never blocks; reads are gated with select().
        self.fd = os.open(f"/dev/video{idx}", os.O_RDWR | os.O_NONBLOCK)
        try:
            self._set_format(width, height, fourcc)
            self._request_buffers(nbuffers)
            self._map_buffers()
        except Exception:
            self.close()
            raise

    # -- private setup helpers -----------------------------------------------

    def _set_format(self, width, height, fourcc):
        fmt = v4l2_format()
        fmt.type = V4L2_BUF_TYPE_VIDEO_CAPTURE
        fmt.pix.width = width
        fmt.pix.height = height
        fmt.pix.pixelformat = _fourcc(fourcc)
        fmt.pix.field = V4L2_FIELD_NONE
        _xioctl(self.fd, VIDIOC_S_FMT, fmt)
        # S_FMT writes back the actually-granted format.
        self.width = fmt.pix.width
        self.height = fmt.pix.height
        self.pixelformat = fmt.pix.pixelformat

    def _request_buffers(self, count):
        req = v4l2_requestbuffers()
        req.count = count
        req.type = V4L2_BUF_TYPE_VIDEO_CAPTURE
        req.memory = V4L2_MEMORY_MMAP
        _xioctl(self.fd, VIDIOC_REQBUFS, req)
        self.nbuffers = req.count  # driver may grant fewer

    def _map_buffers(self):
        for i in range(self.nbuffers):
            buf = v4l2_buffer()
            buf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE
            buf.memory = V4L2_MEMORY_MMAP
            buf.index = i
            _xioctl(self.fd, VIDIOC_QUERYBUF, buf)
            mm = mmap.mmap(
                self.fd,
                buf.length,
                flags=mmap.MAP_SHARED,
                prot=mmap.PROT_READ | mmap.PROT_WRITE,
                offset=buf.m.offset,
            )
            self.buffers.append(mm)

    def _qbuf(self, index):
        """Hand an empty buffer back to the driver to be filled."""
        buf = v4l2_buffer()
        buf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE
        buf.memory = V4L2_MEMORY_MMAP
        buf.index = index
        _xioctl(self.fd, VIDIOC_QBUF, buf)

    # -- public API ----------------------------------------------------------

    def start(self, retries: int = 12, retry_delay: float = 0.02) -> None:
        """STREAMON — reserves the isochronous slot on the USB bus.

        STREAMOFF dequeues all buffers, so we re-queue them first.
        STREAMON can briefly fail with ENOSPC/EBUSY if the previous slot
        hasn't been freed yet, so we retry for a short while.
        """
        if self.streaming:
            return
        for i in range(self.nbuffers):
            self._qbuf(i)
        bt = ctypes.c_int(V4L2_BUF_TYPE_VIDEO_CAPTURE)
        last_exc: Optional[OSError] = None
        for _ in range(retries):
            try:
                _xioctl(self.fd, VIDIOC_STREAMON, bt)
                self.streaming = True
                return
            except OSError as e:
                last_exc = e
                time.sleep(retry_delay)
        raise last_exc  # type: ignore[misc]

    def stop(self) -> None:
        """STREAMOFF — releases the isochronous slot (keeps fd + buffers)."""
        if not self.streaming:
            return
        bt = ctypes.c_int(V4L2_BUF_TYPE_VIDEO_CAPTURE)
        _xioctl(self.fd, VIDIOC_STREAMOFF, bt)
        self.streaming = False

    def grab(self) -> Optional[np.ndarray]:
        """DQBUF one ready buffer, decode to BGR, requeue. None if not ready."""
        buf = v4l2_buffer()
        buf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE
        buf.memory = V4L2_MEMORY_MMAP
        try:
            _xioctl(self.fd, VIDIOC_DQBUF, buf)
        except OSError:
            return None  # EAGAIN: nothing ready
        frame = self._decode(buf.index, buf.bytesused)
        self._qbuf(buf.index)
        return frame

    def read(self, timeout: float = 1.0) -> Optional[np.ndarray]:
        """Block (up to *timeout*) for one frame via ``select()``, then grab."""
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return None
        return self.grab()

    def close(self) -> None:
        """Full teardown: stop streaming, unmap buffers, close fd."""
        try:
            self.stop()
        except Exception:
            pass
        for mm in self.buffers:
            try:
                mm.close()
            except Exception:
                pass
        self.buffers = []
        try:
            req = v4l2_requestbuffers()
            req.count = 0
            req.type = V4L2_BUF_TYPE_VIDEO_CAPTURE
            req.memory = V4L2_MEMORY_MMAP
            _xioctl(self.fd, VIDIOC_REQBUFS, req)
        except Exception:
            pass
        if self.fd >= 0:
            try:
                os.close(self.fd)
            except Exception:
                pass
            self.fd = -1

    # -- decode --------------------------------------------------------------

    def _decode(self, index: int, bytesused: int) -> Optional[np.ndarray]:
        """Convert a raw capture buffer to a BGR image per the negotiated fmt."""
        raw = self.buffers[index]
        if self.pixelformat == FOURCC_MJPG:
            data = np.frombuffer(raw, dtype=np.uint8, count=bytesused)
            return cv2.imdecode(data, cv2.IMREAD_COLOR)
        if self.pixelformat == FOURCC_YUYV:
            n = self.width * self.height * 2
            yuy2 = np.frombuffer(raw, dtype=np.uint8, count=n).reshape(
                self.height, self.width, 2
            )
            return cv2.cvtColor(yuy2, cv2.COLOR_YUV2BGR_YUYV)
        return None  # unsupported format
