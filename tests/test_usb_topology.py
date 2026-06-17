"""Unit-тесты для omniview.usb_topology.present_video_devices.

Проверяет sysfs-обнаружение присутствия видеоустройств — основу для
определения отключения мультиплексируемых камер (вынули USB-хаб).
sysfs подменяется временным каталогом, так что железо не требуется.
"""

from pathlib import Path

import src.omniview.usb_topology as usb_topology


class TestPresentVideoDevices:
    """Тесты present_video_devices."""

    def _patch_sysfs(self, monkeypatch, target: Path):
        """Заставить present_video_devices читать наш каталог вместо sysfs."""
        real_path = Path
        monkeypatch.setattr(
            usb_topology,
            "Path",
            lambda p: target if p == "/sys/class/video4linux" else real_path(p),
        )

    def test_returns_present_video_indices(self, tmp_path, monkeypatch):
        """Возвращает индексы videoN, игнорируя прочие узлы."""
        v4l = tmp_path / "video4linux"
        v4l.mkdir()
        (v4l / "video0").mkdir()
        (v4l / "video2").mkdir()
        (v4l / "media0").mkdir()  # не video* → игнор
        (v4l / "videoX").mkdir()  # не число → игнор

        self._patch_sysfs(monkeypatch, v4l)

        assert usb_topology.present_video_devices() == {0, 2}

    def test_returns_empty_set_when_no_video_nodes(self, tmp_path, monkeypatch):
        """Каталог есть, но видеоузлов нет → пустое множество (не None)."""
        v4l = tmp_path / "video4linux"
        v4l.mkdir()
        (v4l / "media0").mkdir()

        self._patch_sysfs(monkeypatch, v4l)

        assert usb_topology.present_video_devices() == set()

    def test_returns_none_when_sysfs_missing(self, tmp_path, monkeypatch):
        """Если sysfs недоступен (нет каталога) → None."""
        missing = tmp_path / "does_not_exist"
        self._patch_sysfs(monkeypatch, missing)

        assert usb_topology.present_video_devices() is None


class TestPresentCaptureDevices:
    """Тесты present_capture_devices (фильтрация metadata-узлов V4L2)."""

    def test_filters_non_capture_nodes(self, monkeypatch):
        """Отсекает узлы без поддержки видеозахвата (metadata)."""
        monkeypatch.setattr(
            usb_topology, "present_video_devices", lambda: {0, 1, 2, 3}
        )
        # Чётные — capture-узлы, нечётные — metadata.
        monkeypatch.setattr(
            usb_topology,
            "_supports_video_capture",
            lambda idx: idx % 2 == 0,
        )
        assert usb_topology.present_capture_devices() == {0, 2}

    def test_keeps_node_when_capability_unknown(self, monkeypatch):
        """Если способность не определить (None) — узел сохраняется (fail open)."""
        monkeypatch.setattr(usb_topology, "present_video_devices", lambda: {0, 5})
        monkeypatch.setattr(
            usb_topology,
            "_supports_video_capture",
            lambda idx: None if idx == 5 else True,
        )
        assert usb_topology.present_capture_devices() == {0, 5}

    def test_returns_none_when_sysfs_missing(self, monkeypatch):
        """Если sysfs недоступен (None) — пробрасывает None."""
        monkeypatch.setattr(usb_topology, "present_video_devices", lambda: None)
        assert usb_topology.present_capture_devices() is None
