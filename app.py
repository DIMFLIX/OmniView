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

SHOW_GUI = True
MAX_CAMERAS = 10
FRAME_WIDTH = 640
FRAME_HEIGHT = 480
FPS = 30
MIN_UPTIME = 5  # Минимальное время работы камеры перед повторным подключением


class CameraThread(threading.Thread):
    def __init__(self, camera_id, frame_queue, stop_event):
        super().__init__()
        self.camera_id = camera_id
        self.frame_queue = frame_queue
        self.stop_event = stop_event
        self.cap = None
        self.last_frame_time = time.time()
        self.retry_count = 0
        self.max_retries = 3

    def _open_camera(self):
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
                    logger.warning(
                        f"Camera {self.camera_id} open failed (attempt {self.retry_count + 1})"
                    )
                    self.retry_count += 1
                    self.stop_event.wait(1.0)
                    continue

                logger.info(f"Camera {self.camera_id} successfully opened")
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
                                f"Camera {self.camera_id} read error (uptime: {time.time() - start_time:.1f}s)"
                            )
                            self.stop_event.wait(0.1)
                            continue
                        break

            except Exception as e:
                logger.error(f"Camera {self.camera_id} error: {str(e)}")
            finally:
                if self.cap and self.cap.isOpened():
                    self.cap.release()

                if not self.stop_event.is_set() and self.retry_count < self.max_retries:
                    logger.info(f"Camera {self.camera_id} reconnecting...")
                    self.stop_event.wait(2.0)  # Пауза перед повторной попыткой

        logger.info(f"Camera {self.camera_id} thread stopped")


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
        devices = []
        for i in range(MAX_CAMERAS):
            try:
                if sys.platform == "linux":
                    dev_path = f"/dev/video{i}"
                    if os.path.exists(dev_path):
                        # Проверяем, что это действительно камера
                        try:
                            with open(
                                f"/sys/class/video4linux/video{i}/name", "r"
                            ) as f:
                                if "camera" in f.read().lower():
                                    devices.append(i)
                        except:
                            devices.append(i)
                else:
                    # Для Windows просто пробуем все индексы
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
                    if (
                        dev_id not in current_devices
                        and not self.cameras[dev_id]["thread"].is_alive()
                    ):
                        to_remove.append(dev_id)

                for dev_id in to_remove:
                    self._remove_camera(dev_id)

            time.sleep(3)  # Увеличили интервал проверки

    def _add_camera(self, dev_id):
        if dev_id in self.cameras:
            return

        logger.info(f"Adding camera {dev_id}")
        try:
            stop_event = threading.Event()
            thread = CameraThread(
                camera_id=dev_id, frame_queue=self.frame_queue, stop_event=stop_event
            )

            self.cameras[dev_id] = {
                "thread": thread,
                "stop_event": stop_event,
                "last_frame": None,
                "last_update": 0,
            }

            thread.start()

        except Exception as e:
            logger.error(f"Error adding camera {dev_id}: {str(e)}")

    def _remove_camera(self, dev_id):
        if dev_id not in self.cameras:
            return

        try:
            logger.info(f"Removing camera {dev_id}")
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
                            cv2.imshow(f"Camera {dev_id}", frame)
                            manager.active_windows.add(dev_id)
                        except Exception as e:
                            logger.error(f"Display error for camera {dev_id}: {e}")

                    # Закрываем только те окна, камеры которых действительно отключены
                    active_ids = set(frames.keys())
                    for dev_id in list(manager.active_windows):
                        if dev_id not in active_ids:
                            try:
                                cv2.destroyWindow(f"Camera {dev_id}")
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
