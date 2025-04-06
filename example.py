from src.omniview.manager import CameraManager


def frame_callback(camera_id, frame):
    # Your framing
    pass


if __name__ == "__main__":
    manager = CameraManager(
        use_ip_cameras=False,
        show_gui=True,
        max_cameras=10,
        frame_width=640,
        frame_height=480,
        fps=30,
        min_uptime=5.0,
        frame_callback=frame_callback,
    )
    try:
        manager.start()
    except KeyboardInterrupt:
        manager.stop()
