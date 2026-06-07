"""Frame processing filters for the OmniView Dashboard.

Each filter is a callable that takes a BGR ``np.ndarray`` and returns
a BGR ``np.ndarray`` of the same size.  The *Original* filter is a
no-op; the *MediaPipe Hands* filter is only available when the
``mediapipe`` package is installed.
"""

from __future__ import annotations

from typing import Callable

import cv2
import numpy as np

# ---------------------------------------------------------------------------
# Filter implementations
# ---------------------------------------------------------------------------


def filter_original(frame: np.ndarray) -> np.ndarray:
    """Return the frame unchanged."""
    return frame


def filter_grayscale(frame: np.ndarray) -> np.ndarray:
    """Convert to grayscale and back to BGR so the pipeline stays 3-channel."""
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)


def filter_canny(frame: np.ndarray) -> np.ndarray:
    """Apply Canny edge detection; result is converted back to BGR."""
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 80, 160)
    return cv2.cvtColor(edges, cv2.COLOR_GRAY2BGR)


def _make_mediapipe_hands_filter() -> Callable[[np.ndarray], np.ndarray]:
    """Lazily create the MediaPipe Hands filter.

    Returns a callable or *None* if the ``mediapipe`` package is not
    available.
    """
    try:
        import mediapipe as mp  # type: ignore[import-untyped]
    except ImportError:
        return None  # type: ignore[return-value]

    hands = mp.solutions.hands.Hands(
        static_image_mode=False,
        max_num_hands=2,
        min_detection_confidence=0.5,
        min_tracking_confidence=0.5,
    )
    draw = mp.solutions.drawing_utils

    def _filter(frame: np.ndarray) -> np.ndarray:
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        result = hands.process(rgb)
        annotated = frame.copy()
        if result.multi_hand_landmarks:
            for landmarks in result.multi_hand_landmarks:
                draw.draw_landmarks(
                    annotated,
                    landmarks,
                    mp.solutions.hands.HAND_CONNECTIONS,
                )
        return annotated

    return _filter


# ---------------------------------------------------------------------------
# Public registry
# ---------------------------------------------------------------------------

FILTER_NAMES: list[str] = ["Original", "Grayscale", "Canny Edges"]

_FILTER_MAP: dict[str, Callable[[np.ndarray], np.ndarray]] = {
    "Original": filter_original,
    "Grayscale": filter_grayscale,
    "Canny Edges": filter_canny,
}

# Attempt to register the MediaPipe filter
_mediapipe_filter = _make_mediapipe_hands_filter()
if _mediapipe_filter is not None:
    FILTER_NAMES.append("MediaPipe Hands (CPU)")
    _FILTER_MAP["MediaPipe Hands (CPU)"] = _mediapipe_filter


def get_filter(name: str) -> Callable[[np.ndarray], np.ndarray]:
    """Return the filter callable for *name*, falling back to Original."""
    return _FILTER_MAP.get(name, filter_original)
