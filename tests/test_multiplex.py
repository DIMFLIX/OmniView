"""Unit-тесты для omniview.multiplex — горячее отключение камер.

Покрытие:
  - MultiplexGroup.remove_camera: освобождение устройства, очистка структур,
    дозаполнение активного окна parked-камерой.
  - MultiplexScheduler.sync_available: удаление пропавших камер, остановка и
    выбрасывание опустевших групп, сжатие списка мультиплексируемых камер.

Тесты не требуют железа: устройства подменяются MagicMock(spec=V4L2Camera),
а группы планировщика — лёгкими фейками.
"""

import queue
from collections import OrderedDict
from collections import deque
from unittest.mock import MagicMock
from unittest.mock import patch

import numpy as np

from src.omniview.multiplex import MultiplexGroup
from src.omniview.multiplex import MultiplexScheduler
from src.omniview.v4l2_backend import V4L2Camera


def _frame():
    return np.zeros((480, 640, 3), dtype=np.uint8)


def _make_started_group(cameras, slots=2, active=None):
    """Собрать v4l2-группу в «запущенном» состоянии с мок-устройствами."""
    group = MultiplexGroup(
        cameras=list(cameras),
        slots=slots,
        frame_queue=queue.Queue(),
        backend="v4l2",
    )
    devices = {}
    for c in cameras:
        dev = MagicMock(spec=V4L2Camera)
        dev.read.return_value = _frame()
        devices[c] = dev
    # Give the group its own dict so remove_camera's pops don't mutate the
    # `devices` reference the test asserts against.
    group._devices = dict(devices)
    group._frames = {c: _frame() for c in cameras}
    group._last_fresh = {c: 1.0 for c in cameras}
    active = active if active is not None else list(cameras)[:slots]
    group._active = OrderedDict((c, devices[c]) for c in active)
    group._rr = deque(cameras)
    group._started = True
    return group, devices


class TestMultiplexGroupRemoveCamera:
    """Тесты MultiplexGroup.remove_camera."""

    def test_remove_active_camera_promotes_parked(self):
        """Удаление активной камеры освобождает слот → parked-камера оживает."""
        group, devs = _make_started_group([2, 4, 6], slots=2, active=[2, 4])

        group.remove_camera(2)

        # Камера 2 полностью забыта
        assert 2 not in group._devices
        assert 2 not in group._frames
        assert 2 not in group._last_fresh
        assert 2 not in group.cameras
        assert 2 not in group._rr
        devs[2].close.assert_called_once()
        # Освободившийся слот занят выжившей parked-камерой 6
        assert set(group._active.keys()) == {4, 6}
        devs[6].start.assert_called_once()

    def test_remove_parked_camera_no_promotion(self):
        """Удаление parked-камеры не трогает активное окно."""
        group, devs = _make_started_group([2, 4, 6], slots=2, active=[2, 4])

        group.remove_camera(6)

        assert 6 not in group._devices
        assert set(group._active.keys()) == {2, 4}
        devs[6].close.assert_called_once()
        # Слот не освобождался → новых STREAMON нет
        devs[2].start.assert_not_called()
        devs[4].start.assert_not_called()

    def test_remove_unknown_camera_is_noop(self):
        """Удаление неизвестной камеры — без ошибок и без изменений."""
        group, _ = _make_started_group([2, 4], slots=2, active=[2, 4])
        group.remove_camera(99)
        assert set(group._active.keys()) == {2, 4}
        assert group.cameras == [2, 4]

    def test_remove_last_camera_leaves_group_empty(self):
        """Удаление единственной камеры опустошает группу без падения."""
        group, devs = _make_started_group([2], slots=1, active=[2])

        group.remove_camera(2)

        assert group.cameras == []
        assert dict(group._active) == {}
        devs[2].close.assert_called_once()


class _FakeGroup:
    """Лёгкая замена MultiplexGroup для тестов планировщика."""

    def __init__(self, cameras):
        self.cameras = list(cameras)
        self.stopped = False
        self.removed = []

    def remove_camera(self, idx):
        self.removed.append(idx)
        if idx in self.cameras:
            self.cameras.remove(idx)

    def stop(self):
        self.stopped = True


class TestSchedulerSyncAvailable:
    """Тесты MultiplexScheduler.sync_available."""

    @staticmethod
    def _scheduler_with_group(cameras, gid="g", slots=2):
        sched = MultiplexScheduler(frame_queue=queue.Queue())
        group = _FakeGroup(cameras)
        sched._groups = {gid: group}
        sched._group_slots = {gid: slots}
        sched._camera_group = {c: gid for c in cameras}
        sched._multiplex_cameras = list(cameras)
        sched._started = True
        return sched, group

    def test_removes_missing_camera_and_keeps_group(self):
        """Пропавшая камера удаляется, непустая группа сохраняется."""
        sched, group = self._scheduler_with_group([2, 4, 6])

        removed = sched.sync_available({4, 6, 100})

        assert removed == {2}
        assert group.removed == [2]
        assert group.stopped is False
        assert sched.get_multiplex_cameras() == [4, 6]
        assert 2 not in sched._camera_group

    def test_drops_group_when_all_cameras_gone(self):
        """Когда вся группа отключена — она останавливается и выбрасывается."""
        sched, group = self._scheduler_with_group([2, 4])

        removed = sched.sync_available(set())

        assert removed == {2, 4}
        assert group.stopped is True
        assert sched._groups == {}
        assert sched._group_slots == {}
        assert sched.get_multiplex_cameras() == []

    def test_noop_when_all_present(self):
        """Если все камеры на месте — ничего не удаляется."""
        sched, group = self._scheduler_with_group([2, 4])

        removed = sched.sync_available({2, 4, 5})

        assert removed == set()
        assert group.removed == []
        assert group.stopped is False
        assert sched.get_multiplex_cameras() == [2, 4]


class _FakeGroupWithAdd(_FakeGroup):
    """Расширение _FakeGroup с поддержкой add_cameras."""

    def __init__(self, cameras):
        super().__init__(cameras)
        self.added = []

    def add_cameras(self, cameras):
        self.added.extend(cameras)
        for c in cameras:
            if c not in self.cameras:
                self.cameras.append(c)


class TestSchedulerReconfigure:
    """Тесты MultiplexScheduler.reconfigure."""

    @staticmethod
    def _scheduler(cameras, gid="g", slots=2):
        sched = MultiplexScheduler(frame_queue=queue.Queue())
        sched._mode = "force"
        sched._slots = slots
        sched._dwell = 1.5
        sched._settle = 0.2
        sched._backend = "v4l2"
        group = _FakeGroupWithAdd(cameras)
        sched._groups = {gid: group}
        sched._group_slots = {gid: slots}
        sched._camera_group = {c: gid for c in cameras}
        sched._multiplex_cameras = list(cameras)
        sched._started = True
        return sched, group

    def test_adds_new_congested_camera(self):
        """Новая камера на том же хабе добавляется в группу."""
        sched, group = self._scheduler([2, 4])

        with patch(
            "src.omniview.multiplex.needs_multiplexing",
            return_value=({2: "g", 4: "g", 6: "g"}, {"g": 2}, [2, 4, 6]),
        ):
            added, removed = sched.reconfigure([2, 4, 6])

        assert added == {6}
        assert removed == set()
        assert 6 in group.added
        assert sched.get_multiplex_cameras() == [2, 4, 6]

    def test_removes_camera_no_longer_congested(self):
        """Камера убрана из мультиплекса (перестала быть на хабе)."""
        sched, group = self._scheduler([2, 4, 6])

        with patch(
            "src.omniview.multiplex.needs_multiplexing",
            return_value=({2: "g", 4: "g", 6: "g"}, {"g": 0}, []),
        ):
            added, removed = sched.reconfigure([2, 4, 6])

        assert added == set()
        assert removed == {2, 4, 6}
        assert group.stopped is True
        assert sched.get_multiplex_cameras() == []

    def test_noop_when_no_topology_change(self):
        """Топология не изменилась — ничего не происходит."""
        sched, group = self._scheduler([2, 4])

        with patch(
            "src.omniview.multiplex.needs_multiplexing",
            return_value=({2: "g", 4: "g"}, {"g": 2}, [2, 4]),
        ):
            added, removed = sched.reconfigure([2, 4])

        assert added == set()
        assert removed == set()
        assert group.added == []
        assert group.removed == []
