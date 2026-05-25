"""
Unit-тесты для модуля omniview.threads

Покрытие:
  - Инициализация потоков (USB / IP) и хранение параметров
  - Методы идентификации камеры (_get_source, _get_open_args)
  - Конфигурирование камеры (_configure_camera, _additional_config)
  - Открытие камеры с несколькими бэкендами (_try_open_camera)
  - Жизненный цикл потока (run → _process_camera_stream → stop)
  - Механизм повторных подключений (_handle_camera_error)
  - Освобождение ресурсов (_release_camera_resources)
"""

import queue
import threading
import time
from unittest.mock import MagicMock
from unittest.mock import call
from unittest.mock import patch

import cv2
import numpy as np
import pytest

from src.omniview.threads import BaseCameraThread
from src.omniview.threads import IPCameraThread
from src.omniview.threads import USBCameraThread

# ──────────────────────────────────────────────
#  Инициализация и хранение параметров
# ──────────────────────────────────────────────


class TestUSBCameraThreadInit:
    """Тесты инициализации USBCameraThread."""

    def test_default_parameters(self, stop_event, frame_queue):
        """Проверка значений параметров по умолчанию."""
        thread = USBCameraThread(
            camera_id=0,
            frame_queue=frame_queue,
            stop_event=stop_event,
        )
        assert thread.camera_id == 0
        assert thread.frame_width == 640
        assert thread.frame_height == 480
        assert thread.fps == 30
        assert thread.min_uptime == 5.0
        assert thread.cap is None
        assert thread.retry_count == 0
        assert thread.max_retries == 3

    def test_custom_parameters(self, stop_event, frame_queue):
        """Проверка передачи пользовательских параметров."""
        thread = USBCameraThread(
            camera_id=2,
            frame_queue=frame_queue,
            stop_event=stop_event,
            frame_width=1280,
            frame_height=720,
            fps=60,
            min_uptime=10.0,
        )
        assert thread.camera_id == 2
        assert thread.frame_width == 1280
        assert thread.frame_height == 720
        assert thread.fps == 60
        assert thread.min_uptime == 10.0

    def test_is_thread_instance(self, stop_event, frame_queue):
        """Поток должен наследовать threading.Thread."""
        thread = USBCameraThread(
            camera_id=0, frame_queue=frame_queue, stop_event=stop_event
        )
        assert isinstance(thread, threading.Thread)


class TestIPCameraThreadInit:
    """Тесты инициализации IPCameraThread."""

    def test_stores_rtsp_url(self, stop_event, frame_queue):
        """RTSP URL должен быть сохранён в атрибуте."""
        url = "rtsp://192.168.1.100:554/stream"
        thread = IPCameraThread(
            rtsp_url=url,
            camera_id=0,
            frame_queue=frame_queue,
            stop_event=stop_event,
        )
        assert thread.rtsp_url == url

    def test_default_parameters_with_rtsp(self, stop_event, frame_queue):
        """Параметры по умолчанию для IP-камеры."""
        thread = IPCameraThread(
            rtsp_url="rtsp://example.com/live",
            camera_id=5,
            frame_queue=frame_queue,
            stop_event=stop_event,
        )
        assert thread.camera_id == 5
        assert thread.frame_width == 640
        assert thread.frame_height == 480


# ──────────────────────────────────────────────
#  Идентификация камеры
# ──────────────────────────────────────────────


class TestCameraIdentification:
    """Тесты _get_source и _get_open_args."""

    def test_usb_get_source(self, stop_event, frame_queue):
        thread = USBCameraThread(
            camera_id=3, frame_queue=frame_queue, stop_event=stop_event
        )
        assert thread._get_source() == "USB Camera 3"

    def test_usb_get_open_args_returns_camera_id(self, stop_event, frame_queue):
        thread = USBCameraThread(
            camera_id=1, frame_queue=frame_queue, stop_event=stop_event
        )
        # backend аргумент игнорируется для USB
        assert thread._get_open_args(cv2.CAP_V4L2) == 1

    def test_ip_get_source_returns_url(self, stop_event, frame_queue):
        url = "rtsp://10.0.0.1/cam"
        thread = IPCameraThread(
            rtsp_url=url, camera_id=0, frame_queue=frame_queue, stop_event=stop_event
        )
        assert thread._get_source() == url

    def test_ip_get_open_args_returns_url(self, stop_event, frame_queue):
        url = "rtsp://10.0.0.1/cam"
        thread = IPCameraThread(
            rtsp_url=url, camera_id=0, frame_queue=frame_queue, stop_event=stop_event
        )
        assert thread._get_open_args(None) == url


# ──────────────────────────────────────────────
#  Конфигурирование камеры
# ──────────────────────────────────────────────


class TestCameraConfiguration:
    """Тесты _configure_camera и _additional_config."""

    def test_configure_sets_cv2_properties(
        self, stop_event, frame_queue, mock_video_capture
    ):
        """_configure_camera должен устанавливать 4 свойства VideoCapture."""
        thread = USBCameraThread(
            camera_id=0,
            frame_queue=frame_queue,
            stop_event=stop_event,
            frame_width=1280,
            frame_height=720,
            fps=60,
        )
        thread._configure_camera(mock_video_capture)

        expected_calls = [
            call(cv2.CAP_PROP_FRAME_WIDTH, 1280),
            call(cv2.CAP_PROP_FRAME_HEIGHT, 720),
            call(cv2.CAP_PROP_FPS, 60),
            call(cv2.CAP_PROP_BUFFERSIZE, 1),
            # USB additional_config:
            call(cv2.CAP_PROP_AUTOFOCUS, 0),
        ]
        mock_video_capture.set.assert_has_calls(expected_calls, any_order=False)

    def test_usb_additional_config_disables_autofocus(
        self, stop_event, frame_queue, mock_video_capture
    ):
        """USB-камера должна отключать автофокус."""
        thread = USBCameraThread(
            camera_id=0, frame_queue=frame_queue, stop_event=stop_event
        )
        thread._additional_config(mock_video_capture)
        mock_video_capture.set.assert_called_once_with(cv2.CAP_PROP_AUTOFOCUS, 0)

    def test_ip_thread_has_no_additional_config(self, stop_event, frame_queue):
        """IP-камера не должна иметь метод _additional_config."""
        thread = IPCameraThread(
            rtsp_url="rtsp://x",
            camera_id=0,
            frame_queue=frame_queue,
            stop_event=stop_event,
        )
        assert not hasattr(thread, "_additional_config")


# ──────────────────────────────────────────────
#  Открытие камеры (_try_open_camera)
# ──────────────────────────────────────────────


class TestTryOpenCamera:
    """Тесты перебора бэкендов при открытии камеры."""

    def test_opens_with_first_working_backend(
        self, stop_event, frame_queue, mock_video_capture
    ):
        """Камера открывается с первым доступным бэкендом."""
        thread = USBCameraThread(
            camera_id=0, frame_queue=frame_queue, stop_event=stop_event
        )
        with patch("cv2.VideoCapture", return_value=mock_video_capture):
            cap = thread._try_open_camera([cv2.CAP_V4L2])
            assert cap is not None
            assert cap.isOpened()

    def test_falls_back_to_next_backend(self, stop_event, frame_queue):
        """Если первый бэкенд не работает, пробуется следующий."""
        closed_cap = MagicMock()
        closed_cap.isOpened.return_value = False

        open_cap = MagicMock()
        open_cap.isOpened.return_value = True

        thread = USBCameraThread(
            camera_id=0, frame_queue=frame_queue, stop_event=stop_event
        )
        with patch("cv2.VideoCapture", side_effect=[closed_cap, open_cap]):
            cap = thread._try_open_camera([cv2.CAP_V4L2, cv2.CAP_DSHOW])
            assert cap is open_cap

    def test_returns_none_when_all_backends_fail(self, stop_event, frame_queue):
        """Если все бэкенды не работают — возвращается None."""
        closed_cap = MagicMock()
        closed_cap.isOpened.return_value = False

        thread = USBCameraThread(
            camera_id=0, frame_queue=frame_queue, stop_event=stop_event
        )
        with patch("cv2.VideoCapture", return_value=closed_cap):
            cap = thread._try_open_camera([cv2.CAP_V4L2, cv2.CAP_DSHOW])
            assert cap is None

    def test_exception_in_backend_is_handled(self, stop_event, frame_queue):
        """Исключение при открытии бэкенда не прерывает перебор."""
        open_cap = MagicMock()
        open_cap.isOpened.return_value = True

        thread = USBCameraThread(
            camera_id=0, frame_queue=frame_queue, stop_event=stop_event
        )
        with patch(
            "cv2.VideoCapture", side_effect=[RuntimeError("backend fail"), open_cap]
        ):
            cap = thread._try_open_camera([cv2.CAP_V4L2, cv2.CAP_DSHOW])
            assert cap is open_cap


# ──────────────────────────────────────────────
#  IP-камера: _open_camera с RTSP
# ──────────────────────────────────────────────


class TestIPCameraOpen:
    """Тесты открытия IP-камеры напрямую через RTSP URL."""

    def test_open_camera_success(self, stop_event, frame_queue, mock_video_capture):
        """Успешное подключение к RTSP-потоку."""
        thread = IPCameraThread(
            rtsp_url="rtsp://192.168.1.1/live",
            camera_id=0,
            frame_queue=frame_queue,
            stop_event=stop_event,
        )
        with patch("cv2.VideoCapture", return_value=mock_video_capture):
            cap = thread._open_camera()
            assert cap is not None
            assert cap.isOpened()

    def test_open_camera_failure(
        self, stop_event, frame_queue, mock_video_capture_closed
    ):
        """Неудачное подключение возвращает None."""
        thread = IPCameraThread(
            rtsp_url="rtsp://invalid-host/stream",
            camera_id=0,
            frame_queue=frame_queue,
            stop_event=stop_event,
        )
        with patch("cv2.VideoCapture", return_value=mock_video_capture_closed):
            cap = thread._open_camera()
            assert cap is None

    def test_open_camera_exception_returns_none(self, stop_event, frame_queue):
        """Исключение при подключении к RTSP не крашит поток."""
        thread = IPCameraThread(
            rtsp_url="rtsp://broken",
            camera_id=0,
            frame_queue=frame_queue,
            stop_event=stop_event,
        )
        with patch("cv2.VideoCapture", side_effect=Exception("network error")):
            cap = thread._open_camera()
            assert cap is None


# ──────────────────────────────────────────────
#  Освобождение ресурсов
# ──────────────────────────────────────────────


class TestReleaseResources:
    """Тесты _release_camera_resources."""

    def test_releases_opened_capture(self, stop_event, frame_queue, mock_video_capture):
        """Открытый capture должен быть освобождён."""
        thread = USBCameraThread(
            camera_id=0, frame_queue=frame_queue, stop_event=stop_event
        )
        thread.cap = mock_video_capture
        thread._release_camera_resources()
        mock_video_capture.release.assert_called_once()
        assert thread.cap is None

    def test_handles_none_capture(self, stop_event, frame_queue):
        """Не падает, если cap == None."""
        thread = USBCameraThread(
            camera_id=0, frame_queue=frame_queue, stop_event=stop_event
        )
        thread.cap = None
        thread._release_camera_resources()  # не должно быть исключения
        assert thread.cap is None

    def test_handles_closed_capture(self, stop_event, frame_queue):
        """Не вызывает release, если capture уже закрыт."""
        cap = MagicMock()
        cap.isOpened.return_value = False

        thread = USBCameraThread(
            camera_id=0, frame_queue=frame_queue, stop_event=stop_event
        )
        thread.cap = cap
        thread._release_camera_resources()
        cap.release.assert_not_called()
        assert thread.cap is None


# ──────────────────────────────────────────────
#  Обработка ошибок и повторные подключения
# ──────────────────────────────────────────────


class TestHandleCameraError:
    """Тесты _handle_camera_error."""

    def test_increments_retry_count(self, stop_event, frame_queue):
        """Счётчик попыток увеличивается после каждой ошибки."""
        thread = USBCameraThread(
            camera_id=0, frame_queue=frame_queue, stop_event=stop_event
        )
        assert thread.retry_count == 0

        with patch("time.sleep"):
            thread._handle_camera_error("USB Camera 0", RuntimeError("fail"))
        assert thread.retry_count == 1

    def test_does_not_sleep_on_last_retry(self, stop_event, frame_queue):
        """На последней попытке sleep не вызывается (нет смысла ждать)."""
        thread = USBCameraThread(
            camera_id=0, frame_queue=frame_queue, stop_event=stop_event
        )
        thread.retry_count = 2  # max_retries - 1

        with patch("time.sleep") as mock_sleep:
            thread._handle_camera_error("USB Camera 0", RuntimeError("fail"))
        mock_sleep.assert_not_called()
        assert thread.retry_count == 3

    def test_sleeps_between_retries(self, stop_event, frame_queue):
        """Между попытками есть задержка 2 секунды."""
        thread = USBCameraThread(
            camera_id=0, frame_queue=frame_queue, stop_event=stop_event
        )
        thread.retry_count = 0

        with patch("time.sleep") as mock_sleep:
            thread._handle_camera_error("USB Camera 0", RuntimeError("fail"))
        mock_sleep.assert_called_once_with(2.0)

    def test_no_sleep_when_max_retries_is_zero(self, stop_event, frame_queue):
        """При max_retries=0 _handle_camera_error не вызывает sleep."""
        thread = USBCameraThread(
            camera_id=0, frame_queue=frame_queue, stop_event=stop_event
        )
        thread.max_retries = 0

        with patch("time.sleep") as mock_sleep:
            thread._handle_camera_error("USB Camera 0", RuntimeError("fail"))
            mock_sleep.assert_not_called()

    def test_run_skips_open_camera_when_max_retries_is_zero(self, stop_event, frame_queue):
        """При max_retries=0 run() не вызывает _open_camera."""
        thread = USBCameraThread(
            camera_id=0, frame_queue=frame_queue, stop_event=stop_event
        )
        thread.max_retries = 0

        with patch.object(thread, "_open_camera") as mock_open:
            thread.run()
            mock_open.assert_not_called()


# ──────────────────────────────────────────────
#  Обработка видеопотока (_process_camera_stream)
# ──────────────────────────────────────────────


class TestProcessCameraStream:
    """Тесты чтения кадров и помещения их в очередь."""

    def test_frames_put_into_queue(self, stop_event, frame_queue, fake_frame):
        """Успешно прочитанные кадры попадают в очередь."""
        thread = USBCameraThread(
            camera_id=0, frame_queue=frame_queue, stop_event=stop_event
        )

        read_count = 0

        def mock_read():
            nonlocal read_count
            read_count += 1
            if read_count <= 3:
                return True, fake_frame.copy()
            stop_event.set()
            return True, fake_frame.copy()

        cap = MagicMock()
        cap.read.side_effect = mock_read
        thread.cap = cap

        thread._process_camera_stream("USB Camera 0")

        assert frame_queue.qsize() >= 3

    def test_failed_read_after_min_uptime_breaks_loop(self, stop_event, frame_queue):
        """Неудачное чтение после min_uptime прерывает цикл."""
        thread = USBCameraThread(
            camera_id=0,
            frame_queue=frame_queue,
            stop_event=stop_event,
            min_uptime=0.0,  # мгновенный «прошёл min_uptime»
        )

        cap = MagicMock()
        cap.read.return_value = (False, None)
        thread.cap = cap

        # Не должен зависнуть — выходит из цикла при ret == False
        thread._process_camera_stream("USB Camera 0")
        assert frame_queue.qsize() == 0

    def test_resets_retry_count_on_stream_start(self, stop_event, frame_queue):
        """retry_count сбрасывается при успешном старте потока."""
        thread = USBCameraThread(
            camera_id=0,
            frame_queue=frame_queue,
            stop_event=stop_event,
            min_uptime=0.0,
        )
        thread.retry_count = 2

        cap = MagicMock()
        cap.read.return_value = (False, None)
        thread.cap = cap

        thread._process_camera_stream("USB Camera 0")
        assert thread.retry_count == 0


# ──────────────────────────────────────────────
#  Жизненный цикл потока (run)
# ──────────────────────────────────────────────


class TestThreadRunLifecycle:
    """Тесты метода run() — полный цикл работы потока."""

    def test_run_stops_on_stop_event(self, stop_event, frame_queue):
        """Поток завершается при установке stop_event."""
        thread = USBCameraThread(
            camera_id=0, frame_queue=frame_queue, stop_event=stop_event
        )
        stop_event.set()

        # run() должен немедленно выйти
        thread.run()
        assert thread.cap is None

    def test_run_stops_after_max_retries(self, stop_event, frame_queue):
        """Поток завершается после исчерпания попыток переподключения."""
        thread = USBCameraThread(
            camera_id=0, frame_queue=frame_queue, stop_event=stop_event
        )

        with patch.object(thread, "_open_camera", return_value=None), patch(
            "time.sleep"
        ):
            thread.run()

        assert thread.retry_count >= thread.max_retries

    def test_run_releases_resources_on_error(
        self, stop_event, frame_queue, mock_video_capture
    ):
        """Ресурсы камеры освобождаются даже при ошибке."""
        thread = USBCameraThread(
            camera_id=0, frame_queue=frame_queue, stop_event=stop_event
        )

        call_count = 0

        def side_effect_open():
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                stop_event.set()
                return None
            return mock_video_capture

        mock_video_capture.read.side_effect = RuntimeError("read failed")

        with patch.object(thread, "_open_camera", side_effect=side_effect_open), patch(
            "time.sleep"
        ):
            thread.run()

        # После выхода cap должен быть None (ресурсы освобождены)
        assert thread.cap is None


# ──────────────────────────────────────────────
#  DEFAULT_BACKENDS
# ──────────────────────────────────────────────


class TestDefaultBackends:
    """Тесты словаря бэкендов по умолчанию."""

    def test_linux_backend_is_v4l2(self):
        assert cv2.CAP_V4L2 in BaseCameraThread.DEFAULT_BACKENDS["linux"]

    def test_default_backends_exist(self):
        assert "default" in BaseCameraThread.DEFAULT_BACKENDS
        assert len(BaseCameraThread.DEFAULT_BACKENDS["default"]) >= 1

    def test_backends_dict_has_two_keys(self):
        assert set(BaseCameraThread.DEFAULT_BACKENDS.keys()) == {"linux", "default"}


# ──────────────────────────────────────────────
#  Очередь кадров: граничные случаи
# ──────────────────────────────────────────────


class TestFrameQueueBehavior:
    """Тесты поведения потока при переполненной очереди."""

    def test_frame_put_into_specific_queue(self, stop_event, fake_frame):
        """Кадры попадают именно в ту очередь, которая передана при создании."""
        q = queue.Queue(maxsize=5)
        thread = USBCameraThread(camera_id=7, frame_queue=q, stop_event=stop_event)

        call_count = 0

        def mock_read():
            nonlocal call_count
            call_count += 1
            if call_count > 2:
                stop_event.set()
                return False, None
            return True, fake_frame.copy()

        cap = MagicMock()
        cap.read.side_effect = mock_read
        thread.cap = cap
        thread.min_uptime = 0.0

        thread._process_camera_stream("USB Camera 7")

        items = []
        while not q.empty():
            items.append(q.get_nowait())

        assert len(items) == 2
        assert all(cam_id == 7 for cam_id, _ in items)

    def test_frame_contains_correct_numpy_array(self, stop_event, fake_frame):
        """Кадр в очереди — это numpy-массив с правильной размерностью."""
        q = queue.Queue(maxsize=5)
        thread = USBCameraThread(camera_id=0, frame_queue=q, stop_event=stop_event)

        call_count = 0

        def mock_read():
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                stop_event.set()
                return False, None
            return True, fake_frame.copy()

        cap = MagicMock()
        cap.read.side_effect = mock_read
        thread.cap = cap
        thread.min_uptime = 0.0

        thread._process_camera_stream("USB Camera 0")

        cam_id, frame = q.get_nowait()
        assert isinstance(frame, np.ndarray)
        assert frame.shape == (480, 640, 3)
