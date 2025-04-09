from omniview.managers import USBCameraManager


def frame_callback(camera_id, frame):
    # Your framing
    pass


if __name__ == "__main__":
    manager = USBCameraManager(
        show_gui=True, max_cameras=4, frame_callback=frame_callback
    )
    try:
        manager.start()
    except KeyboardInterrupt:
        manager.stop()
