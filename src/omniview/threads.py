import logging
import queue
import sys
import threading
import time
from abc import ABC
from abc import abstractmethod
from typing import Any
from typing import Optional

import cv2


class BaseCameraThread(threading.Thread, ABC):
    """Базовый класс потока для обработки видеопотока с камеры."""

    DEFAULT_BACKENDS = {
        "linux": [cv2.CAP_V4L2],
        "default": [cv2.CAP_DSHOW, cv2.CAP_MSMF],
    }

    def __init__(
        self,
        camera_id: int,
        frame_queue: queue.Queue,
        stop_event: threading.Event,
        frame_width: int = 640,
        frame_height: int = 480,
        fps: int = 30,
        min_uptime: float = 5.0,
    ):
        """Базовый поток для обработки потока с одной камеры

        Args:
            camera_id: Уникальный идентификатор камеры
            frame_queue: Очередь для отправки кадров в основной поток
            stop_event: Событие, сигнализирующее о завершении потока
            frame_width: Желаемая ширина кадра
            frame_height: Желаемая высота кадра
            fps: Целевое количество кадров в секунду
            min_uptime: Минимальное время работы до переподключения (секунды)
        """

        super().__init__()
        self.camera_id = camera_id
        self.frame_queue = frame_queue
        self.stop_event = stop_event
        self.frame_width = frame_width
        self.frame_height = frame_height
        self.fps = fps
        self.min_uptime = min_uptime

        self.cap: cv2.VideoCapture | None = None
        self.last_frame_time = 0
        self.retry_count = 0
        self.max_retries = 3
        self.logger = logging.getLogger(f"{self.__class__.__name__}-{camera_id}")

    def _try_open_camera(self, backends: list) -> Optional[cv2.VideoCapture]:
        """Попытка открыть камеру с использованием различных бэкендов.

        Args:
            backends (list): Список бэкендов для попытки открытия

        Returns:
            Optional[cv2.VideoCapture]: Объект захвата видео или None
        """
        for backend in backends:
            try:
                cap = cv2.VideoCapture(self._get_open_args(backend), backend)
                if cap.isOpened():
                    self._configure_camera(cap)
                    return cap
            except Exception:
                continue
        return None

    def _configure_camera(self, cap: cv2.VideoCapture):
        """Базовая конфигурация параметров камеры.

        Args:
            cap (cv2.VideoCapture): Объект захвата видео
        """
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.frame_width)
        cap.set(cv2.CAP_PROP_FPS, self.fps)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        if hasattr(self, "_additional_config"):
            self._additional_config(cap)

    @abstractmethod
    def _get_open_args(self, backend: int) -> Any:
        """Абстрактный метод получения аргументов для открытия камеры.

        Args:
            backend (int): Используемый бэкенд

        Returns:
            Any: Аргументы для открытия камеры
        """

    @abstractmethod
    def _get_source(self) -> str:
        """Абстрактный метод получения идентификатора источника.

        Returns:
            str: Идентификатор/описание источника
        """

    def run(self):
        """Главный метод потока для обработки видеопотока."""
        while not self.stop_event.is_set() and self.retry_count < self.max_retries:
            source = self._get_source()
            try:
                self.cap = self._open_camera()
                if not self.cap or not self.cap.isOpened():
                    raise RuntimeError(f"Cannot open camera {source}")

                self._process_camera_stream(source)
            except Exception as e:
                self._handle_camera_error(source, e)
            finally:
                self._release_camera_resources()

    def _open_camera(self) -> Optional[cv2.VideoCapture]:
        """Открытие камеры с учетом платформы.

        Returns:
            Optional[cv2.VideoCapture]: Объект захвата видео или None
        """
        backends = self.DEFAULT_BACKENDS.get(
            "linux" if sys.platform == "linux" else "default"
        )
        return self._try_open_camera(backends)

    def _process_camera_stream(self, source: str):
        """Непрерывное чтение и обработка кадров с камеры.

        Args:
            source (str): Идентификатор источника
        """
        self.retry_count = 0
        self.logger.info(f"Camera {source} started")
        start_time = time.time()

        while not self.stop_event.is_set():
            ret, frame = self.cap.read()
            if not ret:
                if time.time() - start_time < self.min_uptime:
                    self.logger.warning(f"Camera {source} frame read error")
                    time.sleep(0.1)
                    continue
                break

            self.frame_queue.put((self.camera_id, frame))
            self.last_frame_time = time.time()

    def _handle_camera_error(self, source: str, error: Exception):
        """Обработка ошибок камеры и планирование переподключения.

        Args:
            source (str): Идентификатор источника
            error (Exception): Произошедшая ошибка
        """
        self.logger.error(f"Camera {source} error: {str(error)}")
        self.retry_count += 1
        if self.retry_count < self.max_retries:
            self.logger.info(f"Reconnecting to {source}...")
            time.sleep(2.0)

    def _release_camera_resources(self):
        """Освобождение ресурсов камеры."""
        if self.cap and self.cap.isOpened():
            self.cap.release()
        self.cap = None


class USBCameraThread(BaseCameraThread):
    """Поток для обработки видеопотока с USB-камеры."""

    def __init__(self, *args, **kwargs):
        """Базовый поток для обработки потока с одной USB-камеры

        Args:
            camera_id: Уникальный идентификатор камеры
            frame_queue: Очередь для отправки кадров в основной поток
            stop_event: Событие, сигнализирующее о завершении потока
            frame_width: Желаемая ширина кадра
            frame_height: Желаемая высота кадра
            fps: Целевое количество кадров в секунду
            min_uptime: Минимальное время работы до переподключения (секунды)
        """
        super().__init__(*args, **kwargs)

    def _get_open_args(self, _) -> Any:
        """Получение аргументов для открытия USB-камеры.

        Returns:
            Any: Идентификатор USB-камеры
        """
        return self.camera_id

    def _get_source(self) -> str:
        """Получение идентификатора USB-камеры.

        Returns:
            str: Строка идентификатора
        """
        return f"USB Camera {self.camera_id}"

    def _additional_config(self, cap: cv2.VideoCapture):
        """Дополнительная конфигурация USB-камеры.

        Args:
            cap (cv2.VideoCapture): Объект захвата видео
        """
        cap.set(cv2.CAP_PROP_AUTOFOCUS, 0)


class IPCameraThread(BaseCameraThread):
    def __init__(self, rtsp_url: str, *args, **kwargs):
        """Базовый поток для обработки потока с одной IP-камеры

        Args:
            rtsp_url: URL-адрес RTSP-потока
            camera_id: Уникальный идентификатор камеры
            frame_queue: Очередь для отправки кадров в основной поток
            stop_event: Событие, сигнализирующее о завершении потока
            frame_width: Желаемая ширина кадра
            frame_height: Желаемая высота кадра
            fps: Целевое количество кадров в секунду
            min_uptime: Минимальное время работы до переподключения (секунды)
        """
        super().__init__(*args, **kwargs)
        self.rtsp_url = rtsp_url

    def _get_open_args(self, _) -> Any:
        """Получение аргументов для открытия IP-камеры.

        Returns:
            Any: URL RTSP потока
        """
        return self.rtsp_url

    def _get_source(self) -> str:
        """Получение идентификатора IP-камеры.

        Returns:
            str: URL RTSP потока
        """
        return self.rtsp_url

    def _open_camera(self) -> Optional[cv2.VideoCapture]:
        """Открытие RTSP потока IP-камеры.

        Returns:
            Optional[cv2.VideoCapture]: Объект захвата видео или None
        """
        try:
            cap = cv2.VideoCapture(self.rtsp_url)
            if cap.isOpened():
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                return cap
        except Exception as e:
            self.logger.error(f"Failed to open IP camera {self.rtsp_url}: {e}")
        return None
