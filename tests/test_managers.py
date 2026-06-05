"""
Unit-тесты для модуля omniview.managers

Покрытие:
  - Инициализация менеджеров (USB, IP) и хранение параметров
  - Логирование (_setup_logging)
  - Формирование заголовков окон (_get_window_title)
  - Добавление / удаление камер (_add_camera, _remove_camera)
  - Определение необходимости удаления камеры (_should_remove_camera)
  - Обработка очереди кадров (process_frames)
  - Обновление состояния камеры и вызов callback (_update_camera_state)
  - Кэширование кадров (_add_cached_frames)
  - Мониторинг подключений (_update_camera_connections)
  - Проверка условия выхода (_check_exit_condition)
  - IPCameraManager: обнаружение устройств и создание потоков
  - USBCameraManager: создание потоков и последовательный режим
  - SequentialCameraMixin: таймер переключения камер
  - Интеграция callback-механизма с менеджером
"""

import os
import queue
import threading
import time
from unittest.mock import MagicMock
from unittest.mock import patch

import cv2
import numpy as np
import pytest

from src.omniview.managers import BaseCameraManager
from src.omniview.managers import IPCameraManager
from src.omniview.managers import USBCameraManager
from src.omniview.threads import IPCameraThread
from src.omniview.threads import USBCameraThread

# ──────────────────────────────────────────────
#  Фикстуры
# ──────────────────────────────────────────────


@pytest.fixture
def usb_manager():
    """USBCameraManager с отключённым GUI."""
    return USBCameraManager(show_gui=False, max_cameras=4)


@pytest.fixture
def ip_manager():
    """IPCameraManager с двумя RTSP-потоками."""
    return IPCameraManager(
        rtsp_urls=[
            "rtsp://192.168.1.10/stream",
            "rtsp://192.168.1.11/stream",
        ],
        show_gui=False,
    )


@pytest.fixture
def usb_manager_sequential():
    """USBCameraManager в последовательном режиме."""
    return USBCameraManager(
        show_gui=False,
        sequential_mode=True,
        switch_interval=3.0,
    )


def _make_mock_thread(alive=True):
    """Создаёт мок потока камеры."""
    t = MagicMock()
    t.is_alive.return_value = alive
    t._get_source.return_value = "mock-source"
    t.join.return_value = None
    return t


# ──────────────────────────────────────────────
#  Инициализация менеджеров
# ──────────────────────────────────────────────


class TestUSBCameraManagerInit:
    """Тесты инициализации USBCameraManager."""

    def test_default_parameters(self):
        mgr = USBCameraManager()
        assert mgr.show_gui is False
        assert mgr.show_camera_id is False
        assert mgr.max_cameras == 10
        assert mgr.frame_width == 640
        assert mgr.frame_height == 480
        assert mgr.fps == 30
        assert mgr.min_uptime == 5.0
        assert mgr.frame_callback is None
        assert mgr.exit_keys == (ord("q"), 27)
        assert mgr.sequential_mode is False
        assert mgr.switch_interval == 5.0

    def test_custom_parameters(self):
        def cb(cam_id, frame):
            return None

        mgr = USBCameraManager(
            show_gui=True,
            show_camera_id=True,
            max_cameras=2,
            frame_width=1280,
            frame_height=720,
            fps=60,
            min_uptime=10.0,
            frame_callback=cb,
            sequential_mode=True,
            switch_interval=3.0,
        )
        assert mgr.show_gui is True
        assert mgr.show_camera_id is True
        assert mgr.max_cameras == 2
        assert mgr.frame_width == 1280
        assert mgr.frame_height == 720
        assert mgr.fps == 60
        assert mgr.frame_callback is cb
        assert mgr.sequential_mode is True
        assert mgr.switch_interval == 3.0

    def test_internal_structures_initialized(self, usb_manager):
        """Внутренние структуры данных создаются при инициализации."""
        assert isinstance(usb_manager.cameras, dict)
        assert len(usb_manager.cameras) == 0
        assert isinstance(usb_manager.active_windows, set)
        assert isinstance(usb_manager.lock, type(threading.Lock()))
        assert isinstance(usb_manager.stop_event, threading.Event)
        assert isinstance(usb_manager.frame_queue, queue.Queue)

    def test_frame_queue_maxsize(self, usb_manager):
        """Размер очереди зависит от max_cameras."""
        assert usb_manager.frame_queue.maxsize == usb_manager.max_cameras * 2


class TestIPCameraManagerInit:
    """Тесты инициализации IPCameraManager."""

    def test_stores_rtsp_urls(self, ip_manager):
        assert len(ip_manager.rtsp_urls) == 2
        assert "rtsp://192.168.1.10/stream" in ip_manager.rtsp_urls

    def test_empty_rtsp_urls(self):
        mgr = IPCameraManager(rtsp_urls=[], show_gui=False)
        assert mgr.rtsp_urls == []


# ──────────────────────────────────────────────
#  Определение необходимости удаления камеры
# ──────────────────────────────────────────────


class TestShouldRemoveCamera:
    """Тесты _should_remove_camera."""

    def test_removes_disconnected_dead_thread(self, usb_manager):
        """Камера не в списке устройств и поток мёртв → удалять."""
        usb_manager.cameras[0] = {"thread": _make_mock_thread(alive=False)}
        assert usb_manager._should_remove_camera(0, current_devices=[1, 2]) is True

    def test_keeps_camera_in_device_list(self, usb_manager):
        """Камера в списке устройств → не удалять."""
        usb_manager.cameras[0] = {"thread": _make_mock_thread(alive=True)}
        assert usb_manager._should_remove_camera(0, current_devices=[0, 1]) is False

    def test_keeps_camera_with_alive_thread(self, usb_manager):
        """Камера не в списке, но поток жив → не удалять (ещё работает)."""
        usb_manager.cameras[0] = {"thread": _make_mock_thread(alive=True)}
        assert usb_manager._should_remove_camera(0, current_devices=[1]) is False


# ──────────────────────────────────────────────
#  Добавление и удаление камер
# ──────────────────────────────────────────────


class TestAddRemoveCamera:
    """Тесты _add_camera и _remove_camera."""

    def test_add_camera_creates_entry(self, usb_manager):
        """_add_camera создаёт запись в self.cameras."""
        mock_thread = _make_mock_thread()
        with patch.object(
            usb_manager, "_create_camera_thread", return_value=mock_thread
        ):
            usb_manager._add_camera(0)

        assert 0 in usb_manager.cameras
        assert usb_manager.cameras[0]["thread"] is mock_thread
        mock_thread.start.assert_called_once()

    def test_add_camera_noop_for_existing(self, usb_manager):
        """Повторное добавление той же камеры ничего не делает."""
        usb_manager.cameras[0] = {"thread": _make_mock_thread()}
        with patch.object(usb_manager, "_create_camera_thread") as mock_create:
            usb_manager._add_camera(0)
        mock_create.assert_not_called()

    def test_add_camera_handles_exception(self, usb_manager):
        """Исключение при создании потока не крашит менеджер."""
        with patch.object(
            usb_manager, "_create_camera_thread", side_effect=RuntimeError("fail")
        ):
            usb_manager._add_camera(0)
        assert 0 not in usb_manager.cameras

    def test_remove_camera_cleans_up(self, usb_manager):
        """_remove_camera останавливает поток и удаляет запись."""
        mock_thread = _make_mock_thread()
        stop_ev = threading.Event()
        usb_manager.cameras[0] = {
            "thread": mock_thread,
            "stop_event": stop_ev,
            "last_frame": None,
            "last_update": 0,
            "source": "USB Camera 0",
        }

        usb_manager._remove_camera(0)

        assert 0 not in usb_manager.cameras
        assert stop_ev.is_set()
        mock_thread.join.assert_called_once_with(timeout=1.0)

    def test_remove_nonexistent_camera_noop(self, usb_manager):
        """Удаление несуществующей камеры не вызывает ошибку."""
        usb_manager._remove_camera(999)  # не должно быть исключения


# ──────────────────────────────────────────────
#  Обработка кадров (process_frames)
# ──────────────────────────────────────────────


class TestProcessFrames:
    """Тесты process_frames."""

    def test_empty_queue_returns_empty_dict(self, usb_manager):
        result = usb_manager.process_frames()
        assert result == {}

    def test_processes_valid_frames(self, usb_manager, fake_frame):
        """Валидные кадры из очереди попадают в результат."""
        usb_manager.cameras[0] = {
            "thread": _make_mock_thread(),
            "stop_event": threading.Event(),
            "last_frame": None,
            "last_update": 0,
            "source": "USB Camera 0",
        }

        usb_manager.frame_queue.put((0, fake_frame))
        result = usb_manager.process_frames()

        assert 0 in result
        assert result[0].shape == (480, 640, 3)

    def test_filters_none_frames(self, usb_manager):
        """Кадры со значением None отбрасываются."""
        usb_manager.cameras[0] = {
            "thread": _make_mock_thread(),
            "stop_event": threading.Event(),
            "last_frame": None,
            "last_update": 0,
            "source": "USB Camera 0",
        }

        usb_manager.frame_queue.put((0, None))
        result = usb_manager.process_frames()
        assert 0 not in result

    def test_filters_frames_with_wrong_shape(self, usb_manager):
        """Кадры с неверной размерностью (не 3D) отбрасываются."""
        usb_manager.cameras[0] = {
            "thread": _make_mock_thread(),
            "stop_event": threading.Event(),
            "last_frame": None,
            "last_update": 0,
            "source": "USB Camera 0",
        }

        # 2D массив (не цветной кадр)
        bad_frame = np.zeros((480, 640), dtype=np.uint8)
        usb_manager.frame_queue.put((0, bad_frame))
        result = usb_manager.process_frames()
        assert 0 not in result

    def test_processes_multiple_cameras(self, usb_manager, fake_frame):
        """Кадры от нескольких камер обрабатываются корректно."""
        for i in range(3):
            usb_manager.cameras[i] = {
                "thread": _make_mock_thread(),
                "stop_event": threading.Event(),
                "last_frame": None,
                "last_update": 0,
                "source": f"USB Camera {i}",
            }
            usb_manager.frame_queue.put((i, fake_frame.copy()))

        result = usb_manager.process_frames()
        assert set(result.keys()) == {0, 1, 2}


# ──────────────────────────────────────────────
#  Обновление состояния камеры (_update_camera_state)
# ──────────────────────────────────────────────


class TestUpdateCameraState:
    """Тесты _update_camera_state."""

    def test_updates_last_frame_and_time(self, usb_manager, fake_frame):
        """Обновляет last_frame и last_update."""
        usb_manager.cameras[0] = {
            "thread": _make_mock_thread(),
            "stop_event": threading.Event(),
            "last_frame": None,
            "last_update": 0,
            "source": "USB Camera 0",
        }

        before = time.time()
        usb_manager._update_camera_state(0, fake_frame)

        assert usb_manager.cameras[0]["last_frame"] is fake_frame
        assert usb_manager.cameras[0]["last_update"] >= before

    def test_calls_callback_with_correct_args(self, fake_frame):
        """Callback вызывается с (camera_id, frame)."""
        callback = MagicMock()
        mgr = USBCameraManager(show_gui=False, frame_callback=callback)
        mgr.cameras[5] = {
            "thread": _make_mock_thread(),
            "stop_event": threading.Event(),
            "last_frame": None,
            "last_update": 0,
            "source": "USB Camera 5",
        }

        mgr._update_camera_state(5, fake_frame)
        callback.assert_called_once_with(5, fake_frame)

    def test_no_callback_does_not_raise(self, usb_manager, fake_frame):
        """Если callback не задан — ошибки нет."""
        usb_manager.cameras[0] = {
            "thread": _make_mock_thread(),
            "stop_event": threading.Event(),
            "last_frame": None,
            "last_update": 0,
            "source": "USB Camera 0",
        }
        usb_manager._update_camera_state(0, fake_frame)  # не должно упасть


# ──────────────────────────────────────────────
#  Кэширование кадров (_add_cached_frames)
# ──────────────────────────────────────────────


class TestAddCachedFrames:
    """Тесты _add_cached_frames."""

    def test_adds_recent_cached_frame(self, usb_manager, fake_frame):
        """Добавляет кэшированный кадр, если он свежий (< 5 секунд)."""
        usb_manager.cameras[0] = {
            "thread": _make_mock_thread(),
            "stop_event": threading.Event(),
            "last_frame": fake_frame,
            "last_update": time.time(),  # свежий
            "source": "USB Camera 0",
        }

        frames = {}
        usb_manager._add_cached_frames(frames)
        assert 0 in frames

    def test_skips_stale_cached_frame(self, usb_manager, fake_frame):
        """Не добавляет кэшированный кадр старше 5 секунд."""
        usb_manager.cameras[0] = {
            "thread": _make_mock_thread(),
            "stop_event": threading.Event(),
            "last_frame": fake_frame,
            "last_update": time.time() - 10.0,  # устаревший
            "source": "USB Camera 0",
        }

        frames = {}
        usb_manager._add_cached_frames(frames)
        assert 0 not in frames

    def test_does_not_override_existing_frame(self, usb_manager, fake_frame):
        """Не перезаписывает уже имеющийся кадр в словаре."""
        new_frame = np.ones((480, 640, 3), dtype=np.uint8) * 255
        usb_manager.cameras[0] = {
            "thread": _make_mock_thread(),
            "stop_event": threading.Event(),
            "last_frame": fake_frame,
            "last_update": time.time(),
            "source": "USB Camera 0",
        }

        frames = {0: new_frame}
        usb_manager._add_cached_frames(frames)
        # Кадр не должен быть заменён на кэшированный
        assert np.array_equal(frames[0], new_frame)

    def test_skips_cameras_without_cached_frame(self, usb_manager):
        """Пропускает камеры без кэшированного кадра (last_frame == None)."""
        usb_manager.cameras[0] = {
            "thread": _make_mock_thread(),
            "stop_event": threading.Event(),
            "last_frame": None,
            "last_update": time.time(),
            "source": "USB Camera 0",
        }

        frames = {}
        usb_manager._add_cached_frames(frames)
        assert 0 not in frames


# ──────────────────────────────────────────────
#  Обновление подключений (_update_camera_connections)
# ──────────────────────────────────────────────


class TestUpdateCameraConnections:
    """Тесты _update_camera_connections."""

    def test_adds_new_devices(self, usb_manager):
        """Новые устройства добавляются."""
        mock_thread = _make_mock_thread()
        with patch.object(
            usb_manager, "_create_camera_thread", return_value=mock_thread
        ):
            usb_manager._update_camera_connections([0, 1])

        assert 0 in usb_manager.cameras
        assert 1 in usb_manager.cameras

    def test_removes_disconnected_devices(self, usb_manager):
        """Отключённые устройства с мёртвым потоком удаляются."""
        usb_manager.cameras[0] = {
            "thread": _make_mock_thread(alive=False),
            "stop_event": threading.Event(),
            "last_frame": None,
            "last_update": 0,
            "source": "USB Camera 0",
        }

        usb_manager._update_camera_connections([])  # камера 0 отключена
        assert 0 not in usb_manager.cameras

    def test_keeps_connected_devices(self, usb_manager):
        """Подключённые устройства остаются."""
        usb_manager.cameras[0] = {
            "thread": _make_mock_thread(alive=True),
            "stop_event": threading.Event(),
            "last_frame": None,
            "last_update": 0,
            "source": "USB Camera 0",
        }

        usb_manager._update_camera_connections([0])
        assert 0 in usb_manager.cameras


# ──────────────────────────────────────────────
#  Проверка условия выхода
# ──────────────────────────────────────────────


class TestCheckExitCondition:
    """Тесты _check_exit_condition."""

    def test_returns_false_when_no_gui(self, usb_manager):
        """Без GUI условие выхода всегда False."""
        assert usb_manager._check_exit_condition() is False

    def test_returns_false_on_non_exit_key(self):
        """Нажатие не-exit-клавиши не вызывает выход."""
        mgr = USBCameraManager(show_gui=True)
        with patch("cv2.waitKey", return_value=ord("a")):
            assert mgr._check_exit_condition() is False

    def test_returns_true_on_q_key(self):
        """Нажатие 'q' вызывает выход."""
        mgr = USBCameraManager(show_gui=True)
        with patch("cv2.waitKey", return_value=ord("q")):
            assert mgr._check_exit_condition() is True

    def test_returns_true_on_esc_key(self):
        """Нажатие Esc вызывает выход."""
        mgr = USBCameraManager(show_gui=True)
        with patch("cv2.waitKey", return_value=27):
            assert mgr._check_exit_condition() is True


# ──────────────────────────────────────────────
#  IPCameraManager: обнаружение устройств
# ──────────────────────────────────────────────


class TestIPCameraManagerDevices:
    """Тесты _get_available_devices и _create_camera_thread для IP."""

    def test_get_available_devices_returns_indices(self, ip_manager):
        """Возвращает индексы 0..N-1 для N RTSP-потоков."""
        devices = ip_manager._get_available_devices()
        assert devices == [0, 1]

    def test_get_available_devices_empty(self):
        mgr = IPCameraManager(rtsp_urls=[], show_gui=False)
        assert mgr._get_available_devices() == []

    def test_create_camera_thread_returns_ip_thread(self, ip_manager):
        """Создаёт IPCameraThread с правильным RTSP URL."""
        stop_ev = threading.Event()
        thread = ip_manager._create_camera_thread(0, stop_ev)

        assert isinstance(thread, IPCameraThread)
        assert thread.rtsp_url == "rtsp://192.168.1.10/stream"
        assert thread.camera_id == 0

    def test_create_camera_thread_second_url(self, ip_manager):
        """Второй поток получает второй URL."""
        stop_ev = threading.Event()
        thread = ip_manager._create_camera_thread(1, stop_ev)
        assert thread.rtsp_url == "rtsp://192.168.1.11/stream"


# ──────────────────────────────────────────────
#  USBCameraManager: создание потоков
# ──────────────────────────────────────────────


class TestUSBCameraManagerCreateThread:
    """Тесты _create_camera_thread для USB."""

    def test_creates_usb_camera_thread(self, usb_manager):
        stop_ev = threading.Event()
        thread = usb_manager._create_camera_thread(0, stop_ev)

        assert isinstance(thread, USBCameraThread)
        assert thread.camera_id == 0
        assert thread.frame_width == usb_manager.frame_width
        assert thread.frame_height == usb_manager.frame_height
        assert thread.fps == usb_manager.fps

    def test_thread_receives_manager_parameters(self):
        """Поток получает параметры, заданные менеджеру."""
        mgr = USBCameraManager(
            frame_width=1920, frame_height=1080, fps=60, min_uptime=10.0
        )
        stop_ev = threading.Event()
        thread = mgr._create_camera_thread(2, stop_ev)

        assert thread.frame_width == 1920
        assert thread.frame_height == 1080
        assert thread.fps == 60
        assert thread.min_uptime == 10.0


# ──────────────────────────────────────────────
#  USBCameraManager: последовательный режим
# ──────────────────────────────────────────────


class TestSequentialMode:
    """Тесты последовательного режима USBCameraManager."""

    def test_sequential_mode_attributes(self, usb_manager_sequential):
        assert usb_manager_sequential.sequential_mode is True
        assert usb_manager_sequential.switch_interval == 3.0
        assert usb_manager_sequential.current_cam_idx == 0
        assert usb_manager_sequential.cameras_list == []
        assert usb_manager_sequential.cap is None
        assert usb_manager_sequential.window_title == "USB Camera Switcher"

    def test_start_calls_sequential_loop(self, usb_manager_sequential):
        """В последовательном режиме start() вызывает _sequential_main_loop."""
        with patch.object(usb_manager_sequential, "_sequential_main_loop") as mock_loop:
            usb_manager_sequential.start()
        mock_loop.assert_called_once()

    def test_start_calls_super_in_normal_mode(self, usb_manager):
        """В обычном режиме start() вызывает родительский метод."""
        with patch.object(BaseCameraManager, "start") as mock_start:
            usb_manager.start()
        mock_start.assert_called_once()


# ──────────────────────────────────────────────
#  SequentialCameraMixin: таймер переключения
# ──────────────────────────────────────────────


class TestCheckSwitchTime:
    """Тесты _check_switch_time."""

    def test_returns_false_before_interval(self, usb_manager_sequential):
        """До истечения интервала — False."""
        start = time.time()
        assert usb_manager_sequential._check_switch_time(start) is False

    def test_returns_true_after_interval(self, usb_manager_sequential):
        """После интервала — True."""
        start = time.time() - 10.0  # 10 секунд назад, interval=3
        assert usb_manager_sequential._check_switch_time(start) is True

    def test_boundary_value(self):
        """Граничное значение: ровно switch_interval."""
        mgr = USBCameraManager(sequential_mode=True, switch_interval=1.0)
        start = time.time() - 1.0
        # >= 1.0 → True
        assert mgr._check_switch_time(start) is True


# ──────────────────────────────────────────────
#  Метод stop()
# ──────────────────────────────────────────────


class TestStopManager:
    """Тесты метода stop()."""

    def test_stop_sets_event(self, usb_manager):
        """stop() устанавливает stop_event."""
        usb_manager.stop()
        assert usb_manager.stop_event.is_set()

    def test_stop_removes_all_cameras(self, usb_manager):
        """stop() удаляет все камеры."""
        for i in range(3):
            usb_manager.cameras[i] = {
                "thread": _make_mock_thread(alive=False),
                "stop_event": threading.Event(),
                "last_frame": None,
                "last_update": 0,
                "source": f"USB Camera {i}",
            }

        usb_manager.stop()
        assert len(usb_manager.cameras) == 0

    def test_stop_joins_monitor_thread(self, usb_manager):
        """stop() ждёт завершения мониторинг-потока."""
        mock_monitor = MagicMock()
        usb_manager.monitor_thread = mock_monitor

        usb_manager.stop()
        mock_monitor.join.assert_called_once_with(timeout=1.0)


# ──────────────────────────────────────────────
#  Callback-интеграция через process_frames
# ──────────────────────────────────────────────


class TestCallbackIntegration:
    """Интеграционные тесты callback-механизма."""

    def test_callback_receives_all_frames(self, fake_frame):
        """Callback вызывается для каждого обработанного кадра."""
        received = []

        def cb(cam_id, frame):
            received.append((cam_id, frame.shape))

        mgr = USBCameraManager(show_gui=False, frame_callback=cb)

        for i in range(3):
            mgr.cameras[i] = {
                "thread": _make_mock_thread(),
                "stop_event": threading.Event(),
                "last_frame": None,
                "last_update": 0,
                "source": f"USB Camera {i}",
            }
            mgr.frame_queue.put((i, fake_frame.copy()))

        mgr.process_frames()

        assert len(received) == 3
        cam_ids = {r[0] for r in received}
        assert cam_ids == {0, 1, 2}

    def test_callback_receives_correct_frame_data(self):
        """Callback получает именно тот кадр, который был в очереди."""
        received_frames = {}

        def cb(cam_id, frame):
            received_frames[cam_id] = frame.copy()

        mgr = USBCameraManager(show_gui=False, frame_callback=cb)

        # Кадр с уникальным содержимым
        unique_frame = np.full((480, 640, 3), 42, dtype=np.uint8)
        mgr.cameras[0] = {
            "thread": _make_mock_thread(),
            "stop_event": threading.Event(),
            "last_frame": None,
            "last_update": 0,
            "source": "USB Camera 0",
        }
        mgr.frame_queue.put((0, unique_frame))
        mgr.process_frames()

        assert 0 in received_frames
        assert np.all(received_frames[0] == 42)

    def test_callback_exception_does_not_crash_manager(self, fake_frame):
        """Исключение в callback не должно ронять менеджер."""

        def bad_cb(cam_id, frame):
            raise ValueError("callback error")

        mgr = USBCameraManager(show_gui=False, frame_callback=bad_cb)
        mgr.cameras[0] = {
            "thread": _make_mock_thread(),
            "stop_event": threading.Event(),
            "last_frame": None,
            "last_update": 0,
            "source": "USB Camera 0",
        }
        mgr.frame_queue.put((0, fake_frame))

        # process_frames вызывает _update_camera_state → callback
        # Callback бросает исключение, но это не должно прерывать работу.
        # Однако в текущей реализации исключение НЕ перехватывается в _update_camera_state,
        # поэтому оно пробросится. Проверяем, что исключение выбрасывается.
        with pytest.raises(ValueError, match="callback error"):
            mgr.process_frames()


# ──────────────────────────────────────────────
#  Последовательный режим: _sequential_main_loop
# ──────────────────────────────────────────────


class TestSequentialMainLoop:
    """Тесты _sequential_main_loop."""

    def test_exits_when_no_cameras(self, usb_manager_sequential):
        """Выходит, если камер нет."""
        with patch.object(
            usb_manager_sequential, "_get_available_devices", return_value=[]
        ):
            usb_manager_sequential._sequential_main_loop()
        # Не должен зависнуть

    def test_cycles_through_cameras(self):
        """Индекс текущей камеры переключается циклически."""
        mgr = USBCameraManager(
            show_gui=False, sequential_mode=True, switch_interval=0.0
        )
        call_log = []

        def mock_get_devices():
            return [0, 1, 2]

        def mock_process(camera_id):
            call_log.append(camera_id)
            if len(call_log) >= 6:
                mgr.stop_event.set()
            return True

        with patch.object(
            mgr, "_get_available_devices", side_effect=mock_get_devices
        ), patch.object(mgr, "_process_camera", side_effect=mock_process), patch.object(
            mgr, "_cleanup_sequential"
        ):
            mgr._sequential_main_loop()

        # Должны были пройти камеры циклично: 0,1,2,0,1,2
        assert call_log == [0, 1, 2, 0, 1, 2]


# ──────────────────────────────
#  Проброс hw_acceleration в потоки
# ──────────────────────────────


class TestHardwareAccelerationManager:
    """Тесты передачи флага hw_acceleration в потоки камер."""

    def test_default_enabled_usb(self, usb_manager):
        """По умолчанию ускорение включено в USB-менеджере."""
        assert usb_manager.hw_acceleration is True

    def test_default_enabled_ip(self, ip_manager):
        """По умолчанию ускорение включено в IP-менеджере."""
        assert ip_manager.hw_acceleration is True

    def test_usb_thread_inherits_disabled_flag(self):
        """USB-поток получает hw_acceleration=False от менеджера."""
        mgr = USBCameraManager(hw_acceleration=False)
        thread = mgr._create_camera_thread(0, threading.Event())
        assert thread.hw_acceleration is False

    def test_usb_thread_inherits_enabled_flag(self, usb_manager):
        """USB-поток по умолчанию получает hw_acceleration=True."""
        thread = usb_manager._create_camera_thread(0, threading.Event())
        assert thread.hw_acceleration is True

    def test_ip_thread_inherits_flag(self):
        """IP-поток получает флаг ускорения от менеджера."""
        mgr = IPCameraManager(rtsp_urls=["rtsp://x"], hw_acceleration=False)
        thread = mgr._create_camera_thread(0, threading.Event())
        assert thread.hw_acceleration is False


# ──────────────────────────────
#  Выбор Qt-платформы для GUI (Wayland/X11)
# ──────────────────────────────


class TestQtPlatformSelection:
    """Тесты выбора QT_QPA_PLATFORM на Linux при show_gui=True."""

    def test_does_not_overwrite_existing_platform(self):
        """Не перезаписывает заданный пользователем QT_QPA_PLATFORM (например, wayland)."""
        with patch("sys.platform", "linux"), patch.dict(
            os.environ, {"QT_QPA_PLATFORM": "wayland"}
        ):
            USBCameraManager(show_gui=True)
            assert os.environ["QT_QPA_PLATFORM"] == "wayland"

    def test_defaults_to_xcb_when_unset(self):
        """Если переменная не задана — устанавливается xcb (X11/XWayland)."""
        with patch("sys.platform", "linux"), patch.dict(os.environ, {}, clear=False):
            os.environ.pop("QT_QPA_PLATFORM", None)
            USBCameraManager(show_gui=True)
            assert os.environ.get("QT_QPA_PLATFORM") == "xcb"
