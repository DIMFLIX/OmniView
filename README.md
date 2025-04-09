
<div align="center">
	<img src=".meta/logo.png">
    <hr/>
    <br/>
	<a href="https://github.com/DIMFLIX/OmniView/issues">
		<img src="https://img.shields.io/github/issues/DIMFLIX/OmniView?color=ffb29b&labelColor=1C2325&style=for-the-badge">
	</a>
	<a href="https://github.com/DIMFLIX/OmniView/stargazers">
		<img src="https://img.shields.io/github/stars/DIMFLIX/OmniView?color=fab387&labelColor=1C2325&style=for-the-badge">
	</a>
	<a href="./LICENSE">
		<img src="https://img.shields.io/github/license/DIMFLIX/OmniView?color=FCA2AA&labelColor=1C2325&style=for-the-badge">
	</a>
	<br>
	<br>
	<a href="./README.ru.md">
		<img src="https://img.shields.io/badge/README-RU-blue?color=cba6f7&labelColor=1C2325&style=for-the-badge">
	</a>
	<a href="./README.md">
		<img src="https://img.shields.io/badge/README-ENG-blue?color=C9CBFF&labelColor=C9CBFF&style=for-the-badge">
	</a>
</div>

# 📝 About the project
A system for simultaneous viewing and processing of streams from multiple cameras (USB/IP) with the ability to integrate into computer vision.
## 🚀 Features
- Support for USB and IP cameras (via RTSP)
- Automatic reconnection in case of connection failure
- Customizable camera parameters (resolution, FPS)
- Multithreaded frame processing
- Flexible callback system for video processing
- Ready-to-use GUI for viewing streams
- Configuration via constructor parameters
## ⚙️ Installation
```bash
pip install omniview
```
## 🛠️ Usage
### 🔌 Basic example for USB cameras
```python
from omniview.managers import USBCameraManager


def frame_callback(camera_id, frame):
    # Your framing
    pass


if __name__ == "__main__":
    manager = USBCameraManager(
        show_gui=True,
        max_cameras=4,
        frame_callback=frame_callback
    )
    try:
        manager.start()
    except KeyboardInterrupt:
        manager.stop()

```

### 🌐 Basic example for IP cameras
```python
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

```

## 📚 API
**Main methods:**
- `start()` - starts the camera manager (blocking call)
- `stop()` - stops all threads correctly
- `process_frames()` - returns dictionary of current frames (ID: frame)

### Class USBCameraManager
**Designer Parameters:**
| Parameter | Type | Default | Description |
|------------------|-----------|--------------|------------------------------|
| show_gui | bool | True | True | Show video windows |
| max_cameras | int | 10 | Max. number of cameras |
| frame_width | int | 640 | frame width |
| frame_height | int | 480 | frame height |
| fps | int | 30 | target FPS |
| min_uptime | float | 5.0 | Min. uptime (sec) |
| frame_callback | function | None | Callback for frame processing|
| exit_keys | tuple | (ord('q'),27)| exit keys |

### Class IPCameraManager
**Builder parameters (Same as USBCameraManager, but with an addition):**
| Parameter | Type | Default | Description |
|------------------|-----------|--------------|------------------------------|
| rtsp_urls | list[str] | [] | List of RTSP URLs |

## 🤝 Project Development
Welcome:
- Bug reports
- Pull requests
- Ideas for improvement
- Usage examples

## 📄 License
The project is distributed under the GNU GPL v3 license.
See the [LICENSE](LICENSE) file for details.