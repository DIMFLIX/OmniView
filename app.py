import cv2
import time
import threading
import sys
import os

SHOW_GUI = True

class CameraManager:
    def __init__(self, show_gui=True, frame_callback=None):
        self.cameras = {}
        self.active_windows = set()
        self.lock = threading.Lock()
        self.running = True
        self.update_interval = 4
        self.show_gui = show_gui
        self.frame_callback = frame_callback
        self.monitor_thread = threading.Thread(target=self._monitor_cameras, daemon=True)
        
        if sys.platform == 'linux':
            self._init_linux()
        else:
            self._init_windows()

        self.monitor_thread.start()

    def _init_linux(self):
        self.backend = cv2.CAP_V4L2
        self.check_devices = self._get_v4l_devices
        self._verify_permissions()

    def _init_windows(self):
        self.backend = cv2.CAP_MSMF
        self.check_devices = self._get_windows_devices

    def _verify_permissions(self):
        if sys.platform == 'linux' and not os.access('/dev/video0', os.R_OK):
            print("Error: Missing camera permissions. Run:")
            print("sudo chmod a+rw /dev/video* && sudo usermod -aG video $USER")
            exit(1)

    def _get_v4l_devices(self):
        devices = []
        for i in range(10):
            try:
                with open(f'/sys/class/video4linux/video{i}/name', 'r') as f:
                    if 'camera' in f.read().lower():
                        devices.append(i)
            except:
                continue
        return devices

    def _get_windows_devices(self):
        devices = []
        for i in range(4):
            try:
                cap = cv2.VideoCapture(i, self.backend)
                if cap.isOpened():
                    devices.append(i)
                    cap.release()
            except:
                continue
        return devices

    def _monitor_cameras(self):
        while self.running:
            current_devices = self.check_devices()
            
            with self.lock:
                for dev_id in current_devices:
                    if dev_id not in self.cameras:
                        self._add_camera(dev_id)

                to_remove = [d for d in self.cameras if d not in current_devices]
                for dev_id in to_remove:
                    self._remove_camera(dev_id)

            time.sleep(self.update_interval)

    def _add_camera(self, dev_id):
        try:
            time.sleep(0.5)
            cap = cv2.VideoCapture(dev_id, self.backend)
            if cap.isOpened():
                if sys.platform == 'linux':
                    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
                    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
                    cap.set(cv2.CAP_PROP_FPS, 30)
                
                self.cameras[dev_id] = cap
                print(f"Camera {dev_id} connected")
                if self.show_gui:
                    self.active_windows.add(dev_id)
        except Exception as e:
            print(f"Connection error: {str(e)}")

    def _remove_camera(self, dev_id):
        if dev_id in self.cameras:
            try:
                self.cameras[dev_id].release()
                if self.show_gui and dev_id in self.active_windows:
                    cv2.destroyWindow(f'Camera {dev_id}')
                    self.active_windows.remove(dev_id)
                    cv2.waitKey(1)
            except Exception as e:
                print(f"Error disconnecting camera {dev_id}: {str(e)}")
            finally:
                del self.cameras[dev_id]
                print(f"Camera {dev_id} disconnected")


    def read_frames(self):
        frames = {}
        with self.lock:
            for dev_id, cap in list(self.cameras.items()):
                try:
                    ret, frame = cap.read()
                    if ret:
                        frames[dev_id] = frame
                        if self.frame_callback:
                            self.frame_callback(dev_id, frame)
                    else:
                        self._remove_camera(dev_id)
                except:
                    self._remove_camera(dev_id)
        return frames

    def stop(self):
        self.running = False
        with self.lock:
            for dev_id in list(self.cameras.keys()):
                self._remove_camera(dev_id)

def main():
    def handle_frame(camera_id, frame):
        """Пример callback-функции для обработки кадров.
        Сюда можно добавить, например, обработку кадров через нейронку.

        Args:
            camera_id (_type_): Айдишник камеры, с которой получаем изображение
            frame (_type_): Кадр видео
        """
        print(f"Received frame from camera {camera_id}, shape: {frame.shape}")

    manager = CameraManager(
        show_gui=SHOW_GUI,
        frame_callback=handle_frame
    )
    
    try:
        while True:
            try:
                # Чтение кадров (будет автоматически вызывать callback)
                frames = manager.read_frames()

                if manager.show_gui:
                    for dev_id, frame in frames.items():
                        try:
                            cv2.imshow(f'Camera {dev_id}', frame)
                        except Exception as e:
                            print(f"Error displaying frame from camera {dev_id}: {e}")
                            manager._remove_camera(dev_id)
                    
                    # Закрываем неактивные окна
                    active_ids = set(frames.keys())
                    for dev_id in list(manager.active_windows):
                        if dev_id not in active_ids:
                            try:
                                cv2.destroyWindow(f'Camera {dev_id}')
                                manager.active_windows.remove(dev_id)
                                cv2.waitKey(1)
                            except Exception:
                                pass

                if cv2.waitKey(1) in (ord('q'), 27):
                    break
            except Exception as e:
                print(f"Main loop error: {e}")
                time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        manager.stop()

if __name__ == "__main__":
    main()