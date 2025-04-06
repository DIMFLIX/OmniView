import logging
import os
import queue
import sys
import threading
import time

import cv2

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("CameraManager")

# Настройки камер
SHOW_GUI = True
MAX_CAMERAS = 10
FRAME_WIDTH = 640
FRAME_HEIGHT = 480
FPS = 30
MIN_UPTIME = 5  # Минимальное время работы камеры перед повторным подключением

# Переключатель между USB и IP камерами
USE_IP_CAMERAS = True  # True - использовать IP камеры, False - USB камеры

# Список URL RTSP потоков IP камер
IP_CAMERAS = []


class CameraThread(threading.Thread):
    def __init__(self, camera_id, frame_queue, stop_event, is_ip_camera=False, rtsp_url=None):
        super().__init__()
        self.camera_id = camera_id
        self.frame_queue = frame_queue
        self.stop_event = stop_event
        self.cap = None
        self.last_frame_time = time.time()
        self.retry_count = 0
        self.max_retries = 3
        self.is_ip_camera = is_ip_camera
        self.rtsp_url = rtsp_url

    def _open_camera(self):
        if self.is_ip_camera:
            # Для IP камер используем RTSP поток
            try:
                cap = cv2.VideoCapture(self.rtsp_url)
                if cap.isOpened():
                    # Для RTSP не все параметры можно установить
                    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                    return cap
            except Exception as e:
                logger.error(f"Failed to open IP camera {self.rtsp_url}: {str(e)}")
                return None
        else:
            # Для USB камер оставляем старую логику
            if sys.platform == "linux":
                attempts = [
                    lambda: cv2.VideoCapture(self.camera_id, cv2.CAP_V4L2),
                ]
            else:
                attempts = [
                    lambda: cv2.VideoCapture(self.camera_id, cv2.CAP_DSHOW),
                    lambda: cv2.VideoCapture(self.camera_id, cv2.CAP_MSMF),
                ]

            for attempt in attempts:
                try:
                    cap = attempt()
                    if cap.isOpened():
                        # Настройка параметров камеры
                        cap.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_WIDTH)
                        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_HEIGHT)
                        cap.set(cv2.CAP_PROP_FPS, FPS)
                        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                        cap.set(cv2.CAP_PROP_AUTOFOCUS, 0)
                        return cap
                except Exception:
                    continue
        return None

    def run(self):
        while not self.stop_event.is_set() and self.retry_count < self.max_retries:
            try:
                self.cap = self._open_camera()

                if not self.cap or not self.cap.isOpened():
                    source = self.rtsp_url if self.is_ip_camera else self.camera_id
                    logger.warning(
                        f"Camera {source} open failed (attempt {self.retry_count + 1})"
                    )
                    self.retry_count += 1
                    self.stop_event.wait(1.0)
                    continue

                source = self.rtsp_url if self.is_ip_camera else self.camera_id
                logger.info(f"Camera {source} successfully opened")
                self.retry_count = 0
                start_time = time.time()

                while not self.stop_event.is_set():
                    ret, frame = self.cap.read()
                    if ret:
                        self.frame_queue.put((self.camera_id, frame))
                        self.last_frame_time = time.time()
                    else:
                        # Если камера перестала работать, но не прошло MIN_UPTIME секунд
                        if time.time() - start_time < MIN_UPTIME:
                            logger.warning(
                                f"Camera {source} read error (uptime: {time.time() - start_time:.1f}s)"
                            )
                            self.stop_event.wait(0.1)
                            continue
                        break

            except Exception as e:
                source = self.rtsp_url if self.is_ip_camera else self.camera_id
                logger.error(f"Camera {source} error: {str(e)}")
            finally:
                if self.cap and self.cap.isOpened():
                    self.cap.release()

                if not self.stop_event.is_set() and self.retry_count < self.max_retries:
                    source = self.rtsp_url if self.is_ip_camera else self.camera_id
                    logger.info(f"Camera {source} reconnecting...")
                    self.stop_event.wait(2.0)  # Пауза перед повторной попыткой

        source = self.rtsp_url if self.is_ip_camera else self.camera_id
        logger.info(f"Camera {source} thread stopped")


class CameraManager:
    def __init__(self, show_gui=True, frame_callback=None):
        self.show_gui = show_gui
        self.frame_callback = frame_callback
        self.active_windows = set()
        self.lock = threading.Lock()
        self.stop_event = threading.Event()

        self.cameras = {}
        self.threads = {}
        self.frame_queue = queue.Queue(maxsize=MAX_CAMERAS * 2)

        self.monitor_thread = threading.Thread(
            target=self._monitor_cameras, daemon=True
        )
        self.monitor_thread.start()

    def _get_available_devices(self):
        if USE_IP_CAMERAS:
            # Для IP камер используем индексы 0..N-1, где N - количество камер в списке
            return list(range(len(IP_CAMERAS)))
        else:
            # Для USB камер используем старую логику поиска подключенных камер
            devices = []
            for i in range(MAX_CAMERAS):
                try:
                    if sys.platform == "linux":
                        dev_path = f"/dev/video{i}"
                        if os.path.exists(dev_path):
                            try:
                                with open(
                                    f"/sys/class/video4linux/video{i}/name", "r"
                                ) as f:
                                    if "camera" in f.read().lower():
                                        devices.append(i)
                            except:
                                devices.append(i)
                    else:
                        devices.append(i)
                except Exception as e:
                    logger.warning(f"Device check error for {i}: {str(e)}")
            return devices

    def _monitor_cameras(self):
        while not self.stop_event.is_set():
            current_devices = self._get_available_devices()

            with self.lock:
                # Добавляем только новые камеры
                for dev_id in current_devices:
                    if dev_id not in self.cameras:
                        self._add_camera(dev_id)

                # Удаляем только те камеры, которых действительно нет
                to_remove = []
                for dev_id in self.cameras:
                    if USE_IP_CAMERAS:
                        # Для IP камер проверяем только что поток остановлен
                        if not self.cameras[dev_id]["thread"].is_alive():
                            to_remove.append(dev_id)
                    else:
                        if (
                            dev_id not in current_devices
                            and not self.cameras[dev_id]["thread"].is_alive()
                        ):
                            to_remove.append(dev_id)

                for dev_id in to_remove:
                    self._remove_camera(dev_id)

            time.sleep(3)

    def _add_camera(self, dev_id):
        if dev_id in self.cameras:
            return

        source = IP_CAMERAS[dev_id] if USE_IP_CAMERAS else dev_id
        logger.info(f"Adding camera {source}")
        try:
            stop_event = threading.Event()
            thread = CameraThread(
                camera_id=dev_id,
                frame_queue=self.frame_queue,
                stop_event=stop_event,
                is_ip_camera=USE_IP_CAMERAS,
                rtsp_url=IP_CAMERAS[dev_id] if USE_IP_CAMERAS else None
            )

            self.cameras[dev_id] = {
                "thread": thread,
                "stop_event": stop_event,
                "last_frame": None,
                "last_update": 0,
                "is_ip_camera": USE_IP_CAMERAS,
                "source": source
            }

            thread.start()

        except Exception as e:
            logger.error(f"Error adding camera {source}: {str(e)}")

    def _remove_camera(self, dev_id):
        if dev_id not in self.cameras:
            return

        try:
            source = self.cameras[dev_id]["source"]
            logger.info(f"Removing camera {source}")
            self.cameras[dev_id]["stop_event"].set()
            self.cameras[dev_id]["thread"].join(timeout=1.0)

            if self.show_gui and dev_id in self.active_windows:
                cv2.destroyWindow(f"Camera {dev_id}")
                self.active_windows.remove(dev_id)
                cv2.waitKey(1)

        except Exception as e:
            logger.error(f"Error removing camera {dev_id}: {str(e)}")
        finally:
            if dev_id in self.cameras:
                del self.cameras[dev_id]

    def process_frames(self):
        frames = {}

        while not self.frame_queue.empty():
            try:
                dev_id, frame = self.frame_queue.get_nowait()
                if frame is not None and len(frame.shape) == 3:
                    frames[dev_id] = frame
                    self.cameras[dev_id]["last_frame"] = frame
                    self.cameras[dev_id]["last_update"] = time.time()

                    if self.frame_callback:
                        self.frame_callback(dev_id, frame)

            except queue.Empty:
                break

        # Добавляем кадры из неактивных, но еще не закрытых камер
        with self.lock:
            for dev_id in list(self.cameras.keys()):
                if (
                    dev_id not in frames
                    and self.cameras[dev_id]["last_frame"] is not None
                    and time.time() - self.cameras[dev_id]["last_update"] < 5.0
                ):
                    frames[dev_id] = self.cameras[dev_id]["last_frame"]

        return frames

    def stop(self):
        logger.info("Stopping camera manager")
        self.stop_event.set()

        for dev_id in list(self.cameras.keys()):
            self._remove_camera(dev_id)

        self.monitor_thread.join(timeout=1.0)


def main():
    def handle_frame(camera_id, frame):
        """Пример callback-функции"""
        pass

    # Фикс для Wayland
    if sys.platform == "linux":
        os.environ["QT_QPA_PLATFORM"] = "xcb"

    manager = CameraManager(show_gui=SHOW_GUI, frame_callback=handle_frame)

    try:
        while True:
            try:
                frames = manager.process_frames()

                if manager.show_gui:
                    for dev_id, frame in frames.items():
                        try:
                            # Отображаем источник камеры в заголовке окна
                            source = manager.cameras[dev_id]["source"]
                            window_title = f"Camera {dev_id} ({'IP' if USE_IP_CAMERAS else 'USB'})"
                            cv2.imshow(window_title, frame)
                            manager.active_windows.add(dev_id)
                        except Exception as e:
                            logger.error(f"Display error for camera {dev_id}: {e}")

                    # Закрываем только те окна, камеры которых действительно отключены
                    active_ids = set(frames.keys())
                    for dev_id in list(manager.active_windows):
                        if dev_id not in active_ids:
                            try:
                                window_title = f"Camera {dev_id} ({'IP' if USE_IP_CAMERAS else 'USB'})"
                                cv2.destroyWindow(window_title)
                                manager.active_windows.remove(dev_id)
                                cv2.waitKey(1)
                            except Exception:
                                pass

                if cv2.waitKey(1) in (ord("q"), 27):
                    break

            except Exception as e:
                logger.error(f"Main loop error: {e}")
                time.sleep(1)

    except KeyboardInterrupt:
        pass
    finally:
        manager.stop()


if __name__ == "__main__":
    main()
