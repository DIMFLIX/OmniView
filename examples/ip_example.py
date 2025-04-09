from omniview.managers import IPCameraManager


def frame_callback(camera_id, frame):
    # Your framing
    pass


if __name__ == "__main__":
    manager = IPCameraManager(
        show_gui=True,
        rtsp_urls=[
            "rtsp://admin:12345@192.168.0.1:9090",
        ],
        max_cameras=4,
        frame_callback=frame_callback
    )
    try:
        manager.start()
    except KeyboardInterrupt:
        manager.stop()