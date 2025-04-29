import logging
import os
import queue
import sys
import threading
import time
from abc import ABC
from abc import abstractmethod
from typing import Any
from typing import Callable
from typing import Dict
from typing import List
from typing import Optional

import cv2

from .threads import BaseCameraThread
from .threads import IPCameraThread
from .threads import USBCameraThread


class BaseCameraManager(ABC):
    """Базовый менеджер для управления несколькими видеопотоками с камер.

    Обеспечивает общую функциональность для работы с USB и IP-камерами,
    включая автоматическое переподключение, обработку кадров и отображение в GUI.
    """

    def __init__(
        self,
        show_gui: bool = False,
        show_camera_id: bool = False,
        max_cameras: int = 10,
        frame_width: int = 640,
        frame_height: int = 480,
        fps: int = 30,
        min_uptime: float = 5.0,
        frame_callback: Optional[Callable[[int, Any], None]] = None,
        exit_keys: tuple = (ord("q"), 27),
    ):
        """Инициализация базового менеджера камер.

        Args:
            show_gui (bool): Отображать окна с видеопотоками
            show_camera_id (bool): Показывать ID камеры на кадре
            max_cameras (int): Максимальное количество камер
            frame_width (int): Ширина кадра
            frame_height (int): Высота кадра
            fps (int): Целевое количество кадров в секунду
            min_uptime (float): Минимальное время работы перед переподключением (сек)
            frame_callback (Callable): Функция обработки кадров
            exit_keys (tuple): Клавиши для выхода из приложения
        """
        self._setup_logging()

        self.show_gui = show_gui
        self.show_camera_id = show_camera_id
        self.max_cameras = max_cameras
        self.frame_width = frame_width
        self.frame_height = frame_height
        self.fps = fps
        self.min_uptime = min_uptime
        self.frame_callback = frame_callback
        self.exit_keys = exit_keys

        self.active_windows = set()
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.cameras: dict[int, dict] = {}
        self.frame_queue = queue.Queue(maxsize=self.max_cameras * 2)

        if self.show_gui and sys.platform == "linux":
            os.environ["QT_QPA_PLATFORM"] = "xcb"

    def _setup_logging(self):
        """Настройка системы логирования для менеджера."""
        self.logger = logging.getLogger(self.__class__.__name__)
        self.logger.setLevel(logging.INFO)
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("[%(levelname)s] %(name)s: %(message)s"))
        self.logger.addHandler(handler)

    @abstractmethod
    def _get_available_devices(self) -> List[int]:
        """Абстрактный метод для получения списка доступных устройств.

        Returns:
            List[int]: Список идентификаторов доступных камер
        """

    @abstractmethod
    def _create_camera_thread(self, camera_id: int) -> threading.Thread:
        """Абстрактный метод для создания потока обработки камеры.

        Args:
            camera_id (int): Идентификатор камеры

        Returns:
            threading.Thread: Поток для обработки видеопотока
        """

    def start(self):
        """Запуск менеджера камер и начало обработки видеопотоков."""
        self.monitor_thread = threading.Thread(
            target=self._monitor_cameras, daemon=True
        )
        self.monitor_thread.start()
        self._main_loop()

    def stop(self):
        """Остановка всех процессов и освобождение ресурсов."""
        self.stop_event.set()

        for dev_id in list(self.cameras.keys()):
            self._remove_camera(dev_id)

        if hasattr(self, "monitor_thread"):
            self.monitor_thread.join(timeout=1.0)

        self._cleanup_gui_resources()

    def _cleanup_gui_resources(self):
        """Очистка ресурсов, связанных с графическим интерфейсом."""
        if not self.show_gui:
            return

        for window in list(self.active_windows):
            try:
                cv2.destroyWindow(window)
                cv2.waitKey(1)
            except Exception:
                pass
        self.active_windows.clear()

    def _monitor_cameras(self):
        """Мониторинг состояния подключенных камер и управление подключениями."""
        while not self.stop_event.is_set():
            current_devices = self._get_available_devices()

            with self.lock:
                self._update_camera_connections(current_devices)

            time.sleep(3)

    def _update_camera_connections(self, current_devices: List[int]):
        """Обновление списка подключенных камер.

        Args:
            current_devices (List[int]): Список доступных в данный момент камер
        """
        # Добавляем новые подключенные камеры
        for dev_id in current_devices:
            if dev_id not in self.cameras:
                self._add_camera(dev_id)

        # Удаляем отключенные камеры
        for dev_id in list(self.cameras.keys()):
            if self._should_remove_camera(dev_id, current_devices):
                self._remove_camera(dev_id)

    def _should_remove_camera(self, dev_id: int, current_devices: List[int]) -> bool:
        """Проверка необходимости удаления камеры из списка активных.

        Args:
            dev_id (int): Идентификатор камеры
            current_devices (List[int]): Список доступных камер

        Returns:
            bool: True если камеру нужно удалить
        """
        return (
            dev_id not in current_devices
            and not self.cameras[dev_id]["thread"].is_alive()
        )

    def _add_camera(self, dev_id: int):
        """Добавление новой камеры в список активных.

        Args:
            dev_id (int): Идентификатор камеры
        """
        if dev_id in self.cameras:
            return

        self.logger.info(f"Adding camera {dev_id}")

        try:
            stop_event = threading.Event()
            thread = self._create_camera_thread(dev_id, stop_event)

            self.cameras[dev_id] = {
                "thread": thread,
                "stop_event": stop_event,
                "last_frame": None,
                "last_update": 0,
                "source": thread._get_source(),
            }

            thread.start()
        except Exception as e:
            self.logger.error(f"Error adding camera {dev_id}: {str(e)}")

    def _remove_camera(self, dev_id: int):
        """Удаление камеры из списка активных.

        Args:
            dev_id (int): Идентификатор камеры
        """
        if dev_id not in self.cameras:
            return

        source = self.cameras[dev_id]["source"]
        try:
            self.logger.info(f"Removing camera {source}")
            self.cameras[dev_id]["stop_event"].set()
            self.cameras[dev_id]["thread"].join(timeout=1.0)

            if self.show_gui:
                window_title = self._get_window_title(dev_id)
                if window_title in self.active_windows:
                    cv2.destroyWindow(window_title)
                    self.active_windows.remove(window_title)
                    cv2.waitKey(1)

        except Exception as e:
            self.logger.error(f"Error removing camera {dev_id}: {str(e)}")
        finally:
            if dev_id in self.cameras:
                del self.cameras[dev_id]

    def _get_window_title(self, dev_id: int) -> str:
        """Генерация заголовка окна для камеры.

        Args:
            dev_id (int): Идентификатор камеры

        Returns:
            str: Заголовок окна
        """
        camera_type = self.__class__.__name__.replace("CameraManager", "")
        source = (
            self.cameras[dev_id]["source"] if dev_id in self.cameras else str(dev_id)
        )
        return f"Camera {dev_id} ({camera_type}): {source}"

    def process_frames(self) -> Dict[int, Any]:
        """Обработка всех доступных кадров из очереди.

        Returns:
            Dict[int, Any]: Словарь с кадрами (ключ - ID камеры)
        """
        frames = {}

        while not self.frame_queue.empty():
            try:
                dev_id, frame = self.frame_queue.get_nowait()
                if frame is not None and len(frame.shape) == 3:
                    frames[dev_id] = frame
                    self._update_camera_state(dev_id, frame)
            except queue.Empty:
                break

        self._add_cached_frames(frames)
        return frames

    def _update_camera_state(self, dev_id: int, frame: Any):
        """Обновление состояния камеры новым кадром.

        Args:
            dev_id (int): Идентификатор камеры
            frame (Any): Кадр видео
        """
        self.cameras[dev_id]["last_frame"] = frame
        self.cameras[dev_id]["last_update"] = time.time()

        if self.frame_callback:
            self.frame_callback(dev_id, frame)

    def _add_cached_frames(self, frames: Dict[int, Any]):
        """Добавление кэшированных кадров от неактивных камер.

        Args:
            frames (Dict[int, Any]): Словарь для добавления кадров
        """
        with self.lock:
            for dev_id in list(self.cameras.keys()):
                if (
                    dev_id not in frames
                    and self.cameras[dev_id]["last_frame"] is not None
                    and time.time() - self.cameras[dev_id]["last_update"] < 5.0
                ):
                    frames[dev_id] = self.cameras[dev_id]["last_frame"]

    def _main_loop(self):
        """Главный цикл обработки видеопотоков."""
        try:
            while not self.stop_event.is_set():
                try:
                    self._process_frame_iteration()
                except Exception as e:
                    self.logger.error(f"Main loop error: {e}")
                    time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def _process_frame_iteration(self):
        """Обработка одной итерации главного цикла."""
        frames = self.process_frames()

        if self.show_gui:
            self._update_gui_windows(frames)

        if self._check_exit_condition():
            self.stop_event.set()

    def _show_camera_id_in_frame(self, frame, camera_id: int):
        """Добавление ID камеры на кадр.

        Args:
            frame: Кадр видео
            camera_id (int): Идентификатор камеры
        """
        cv2.putText(
            frame,
            f"Camera {camera_id}",
            (10, 30),
            cv2.FONT_HERSHEY_SIMPLEX,
            1,
            (0, 255, 0),
            2,
        )

    def _update_gui_windows(self, frames: Dict[int, Any]):
        """Обновление окон GUI новыми кадрами.

        Args:
            frames (Dict[int, Any]): Словарь с кадрами
        """
        for dev_id, frame in frames.items():
            try:
                window_title = self._get_window_title(dev_id)
                if self.show_camera_id:
                    self._show_camera_id_in_frame(frame, dev_id)
                cv2.imshow(window_title, frame)
                self.active_windows.add(window_title)
            except Exception as e:
                self.logger.error(f"Display error for camera {dev_id}: {e}")

        self._cleanup_inactive_windows(frames.keys())

    def _cleanup_inactive_windows(self, active_ids: set):
        """Очистка окон неактивных камер.

        Args:
            active_ids (set): Множество ID активных камер
        """
        for window_title in list(self.active_windows):
            dev_id = int(window_title.split()[1])
            if dev_id not in active_ids:
                try:
                    cv2.destroyWindow(window_title)
                    self.active_windows.remove(window_title)
                    cv2.waitKey(1)
                except Exception:
                    pass

    def _check_exit_condition(self) -> bool:
        """Проверка условий для выхода из приложения.

        Returns:
            bool: True если нужно завершить работу
        """
        if not self.show_gui:
            return False

        key = cv2.waitKey(1)
        return key in self.exit_keys


class SequentialCameraMixin:
    """Миксин, который добавляет функциональность последовательного переключения камер в менеджеры камер.

    Этот миксин позволяет отображать камеры одну за другой в циклическом порядке,
    с настраиваемым интервалом переключения. Он предназначен для работы с менеджерами камер,
    наследующими от `BaseCameraManager`.

    Требуется, чтобы класс хоста реализовал:
        Атрибуты:
            - frame_callback: Optional[Callable]
            - stop_event: threading.Event
            - cameras_list: List[int]
            - current_cam_idx: int
            - exit_keys: tuple
            - cap: cv2.VideoCapture
            - frame_width: int
            - frame_height: int
            - fps: int
            - show_gui: bool
            - show_camera_id: bool
            - window_title: str
            - switch_interval: float

        Методы:
            - _get_available_devices()
            - _show_camera_id_in_frame()
    """

    def _open_camera(self, camera_id: int) -> Optional[cv2.VideoCapture]:
        """Открытие камеры с учетом платформы.

        Args:
            camera_id (int): Идентификатор камеры

        Returns:
            Optional[cv2.VideoCapture]: Объект захвата видео или None
        """
        backends = ["linux"] if sys.platform == "linux" else ["default"]
        for backend in backends:
            for api in BaseCameraThread.DEFAULT_BACKENDS[backend]:
                cap = cv2.VideoCapture(camera_id, api)
                if cap.isOpened():
                    return cap
        return None

    def _sequential_main_loop(self):
        """Главный цикл для последовательного отображения камер."""
        self.cameras_list = self._get_available_devices()
        if not self.cameras_list:
            self.logger.error("No USB cameras found")
            return

        self.logger.info(f"Available cameras: {self.cameras_list}")

        try:
            while not self.stop_event.is_set():
                camera_id = self.cameras_list[self.current_cam_idx]
                success = self._process_camera(camera_id)

                if not success and not self.stop_event.is_set():
                    self.logger.warning(f"Skipping camera {camera_id}")

                self.current_cam_idx = (self.current_cam_idx + 1) % len(
                    self.cameras_list
                )

        except Exception as e:
            self.logger.error(f"Sequential mode error: {str(e)}")
        finally:
            self._cleanup_sequential()

    def _check_exit_keys(self):
        """Обработка нажатий клавиш для выхода."""
        key = cv2.waitKey(1)
        if key in self.exit_keys:
            self.stop_event.set()

    def _process_camera(self, camera_id: int) -> bool:
        """Обработка одной камеры в течение интервала переключения.

        Args:
            camera_id (int): Идентификатор камеры

        Returns:
            bool: Успешность обработки
        """
        self.cap = self._open_camera(camera_id)
        if not self.cap or not self.cap.isOpened():
            return False

        try:
            self._configure_camera()
            start_time = time.time()

            while not self.stop_event.is_set():
                self._handle_frame(camera_id)
                if self._check_switch_time(start_time):
                    break

            return True
        except Exception as e:
            self.logger.error(f"Camera {camera_id} error: {str(e)}")
            return False
        finally:
            self.cap.release()
            self.cap = None

    def _configure_camera(self):
        """Конфигурация параметров камеры."""
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.frame_width)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.frame_height)
        self.cap.set(cv2.CAP_PROP_FPS, self.fps)
        self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    def _handle_frame(self, camera_id: int):
        """Обработка одного кадра.

        Args:
            camera_id (int): Идентификатор камеры
        """
        ret, frame = self.cap.read()
        if not ret:
            return

        if self.show_gui:
            self._display_frame(camera_id, frame)

        if self.frame_callback:
            self.frame_callback(camera_id, frame)

        self._check_exit_keys()

    def _display_frame(self, camera_id: int, frame):
        """Отображение кадра в GUI.

        Args:
            camera_id (int): Идентификатор камеры
            frame: Кадр видео
        """
        if self.show_camera_id:
            self._show_camera_id_in_frame(frame, camera_id)

        cv2.imshow(self.window_title, frame)

    def _check_switch_time(self, start_time: float) -> bool:
        """Проверка истечения интервала переключения.

        Args:
            start_time (float): Время начала отображения

        Returns:
            bool: True если интервал истек
        """
        return (time.time() - start_time) >= self.switch_interval

    def _cleanup_sequential(self):
        """Очистка ресурсов последовательного режима."""
        if self.cap and self.cap.isOpened():
            self.cap.release()
        if self.show_gui:
            cv2.destroyAllWindows()
        self.stop()


class USBCameraManager(SequentialCameraMixin, BaseCameraManager):
    """Менеджер для работы с несколькими потоками USB-камер

    Args:
        show_gui: Отображать видеоокна
        show_camera_id: Добавляет надпись с идентификатором камеры в кадр
        max_cameras: Максимальное количество камер для обработки
        frame_width: Желаемая ширина кадра
        frame_height: Желаемая высота кадра
        fps: Целевое количество кадров в секунду
        min_uptime: Минимальное время работы до переподключения (секунды)
        frame_callback: Функция обратного вызова для обработки кадров
        exit_keys: Клавиши клавиатуры для выхода из приложения
        sequential_mode: Метод показа камер по очереди
        switch_interval: Время, через которое камеры будут меняться. Работает, только если выбран режим sequential_mode
    """

    def __init__(
        self,
        *args,
        sequential_mode: bool = False,
        switch_interval: float = 5.0,
        **kwargs,
    ):
        """Инициализация менеджера USB-камер.

        Args:
            sequential_mode (bool): Режим последовательного отображения
            switch_interval (float): Интервал переключения камер (сек)
        """
        super().__init__(*args, **kwargs)
        self.sequential_mode = sequential_mode
        self.switch_interval = switch_interval
        self.current_cam_idx = 0
        self.cameras_list = []
        self.cap = None
        self.window_title = "USB Camera Switcher"

    def start(self):
        """Запуск менеджера в выбранном режиме."""
        if self.sequential_mode:
            self._sequential_main_loop()
        else:
            super().start()

    def _get_available_devices(self) -> List[int]:
        """Получение списка доступных USB-камер.

        Returns:
            List[int]: Список ID доступных камер
        """
        devices = []

        for i in range(self.max_cameras):
            cap = cv2.VideoCapture(i, cv2.CAP_V4L2)
            if cap.isOpened():
                devices.append(i)
                cap.release()
            else:
                self.logger.info(f"The camera with index {i} is not available")
        return devices

    def _create_camera_thread(
        self, camera_id: int, stop_event: threading.Event
    ) -> threading.Thread:
        """Создание потока для обработки USB-камеры.

        Args:
            camera_id (int): Идентификатор камеры
            stop_event (threading.Event): Событие для остановки потока

        Returns:
            threading.Thread: Поток обработки камеры
        """
        return USBCameraThread(
            camera_id=camera_id,
            frame_queue=self.frame_queue,
            stop_event=stop_event,
            frame_width=self.frame_width,
            frame_height=self.frame_height,
            fps=self.fps,
            min_uptime=self.min_uptime,
        )


class IPCameraManager(BaseCameraManager):
    """Менеджер для работы с несколькими потоками IP-камер

    Args:
        rtsp_urls: URL-адреса RTSP-потоков
        show_gui: Отображать видеоокна
        max_cameras: Максимальное количество камер для обработки
        frame_width: Желаемая ширина кадра
        frame_height: Желаемая высота кадра
        fps: Целевое количество кадров в секунду
        min_uptime: Минимальное время работы до переподключения (секунды)
        frame_callback: Функция обратного вызова для обработки кадров
        exit_keys: Клавиши клавиатуры для выхода из приложения
    """

    def __init__(self, rtsp_urls: List[str], *args, **kwargs):
        """Инициализация менеджера IP-камер.

        Args:
            rtsp_urls (List[str]): Список URL RTSP потоков
        """
        super().__init__(*args, **kwargs)
        self.rtsp_urls = rtsp_urls

    def _get_available_devices(self) -> List[int]:
        """Получение списка доступных IP-камер.

        Returns:
            List[int]: Список индексов доступных потоков
        """
        return list(range(len(self.rtsp_urls)))

    def _create_camera_thread(
        self, camera_id: int, stop_event: threading.Event
    ) -> threading.Thread:
        """Создание потока для обработки IP-камеры.

        Args:
            camera_id (int): Идентификатор камеры
            stop_event (threading.Event): Событие для остановки потока

        Returns:
            threading.Thread: Поток обработки камеры
        """
        return IPCameraThread(
            rtsp_url=self.rtsp_urls[camera_id],
            camera_id=camera_id,
            frame_queue=self.frame_queue,
            stop_event=stop_event,
            frame_width=self.frame_width,
            frame_height=self.frame_height,
            fps=self.fps,
            min_uptime=self.min_uptime,
        )
