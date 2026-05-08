import queue
import threading
from unittest.mock import MagicMock
from unittest.mock import patch

import numpy as np
import pytest


@pytest.fixture
def stop_event():
    """Threading event used to signal stop to camera threads."""
    return threading.Event()


@pytest.fixture
def frame_queue():
    """Queue for passing frames between camera threads and manager."""
    return queue.Queue(maxsize=20)


@pytest.fixture
def fake_frame():
    """A realistic 480p BGR frame (3-channel numpy array)."""
    return np.zeros((480, 640, 3), dtype=np.uint8)


@pytest.fixture
def fake_frame_720p():
    """A realistic 720p BGR frame."""
    return np.zeros((720, 1280, 3), dtype=np.uint8)


@pytest.fixture
def mock_video_capture():
    """A mock cv2.VideoCapture that simulates an opened camera."""
    cap = MagicMock()
    cap.isOpened.return_value = True
    cap.read.return_value = (True, np.zeros((480, 640, 3), dtype=np.uint8))
    cap.set.return_value = True
    cap.release.return_value = None
    return cap


@pytest.fixture
def mock_video_capture_closed():
    """A mock cv2.VideoCapture that simulates a camera that failed to open."""
    cap = MagicMock()
    cap.isOpened.return_value = False
    return cap
