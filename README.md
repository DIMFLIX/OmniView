
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
    <br>
    <br>

---

[About the project](#about-project) • [Installation](#installation) • [Usage](#usage) • [API](#api) • [Legal status](#legal-status)


<br>
</div>

# <a name="about-project"></a>📝 About the project
A system for simultaneous viewing and processing of streams from multiple cameras (USB/IP) with the ability to integrate into computer vision.
## 🚀 Features
- Support for USB and IP cameras (via RTSP)
- Automatic reconnection in case of connection failure
- Customizable camera parameters (resolution, FPS)
- Multithreaded frame processing
- Hardware-accelerated decoding (D3D11 on Windows, VAAPI on Linux) with automatic software fallback
- Flexible callback system for video processing
- Ready-to-use GUI for viewing streams
- Configuration via constructor parameters
## <a name="installation"></a>⚙️ Installation
```bash
pip install omniview
```
## <a name="usage"></a>🛠️ Usage
### 🔌 Basic example for USB cameras
```python
from omniview.managers import USBCameraManager


def frame_callback(camera_id, frame):
    # Your framing
    pass


if __name__ == "__main__":
    manager = USBCameraManager(
        show_gui=True,
        show_camera_id=True,
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
        frame_callback=frame_callback
    )
    try:
        manager.start()
    except KeyboardInterrupt:
        manager.stop()

```

## <a name="api"></a>📚 API
**Main methods:**
- `start()` - starts the camera manager (blocking call)
- `stop()` - stops all threads correctly

### Class USBCameraManager
**Designer Parameters:**
| Parameter       | Type     | Default       | Description                                                                             |
| --------------- | -------- | ------------- | --------------------------------------------------------------------------------------- |
| show_gui        | bool     | False         | Show video windows                                                                      |
| show_camera_id  | bool     | False         | Adds a caption with the camera ID to the frame                                          |
| max_cameras     | int      | 10            | Max. number of cameras                                                                  |
| frame_width     | int      | 640           | frame width                                                                             |
| frame_height    | int      | 480           | frame height                                                                            |
| fps             | int      | 30            | target FPS                                                                              |
| min_uptime      | float    | 5.0           | Min. uptime (sec)                                                                       |
| frame_callback  | function | None          | Callback for frame processing                                                           |
| exit_keys       | tuple    | (ord('q'),27) | exit keys                                                                               |
| hw_acceleration | bool     | True          | Use GPU video decoding when available (D3D11/VAAPI); falls back to software             |
| sequential_mode | bool     | False         | Method to show the cameras one by one                                                   |
| switch_interval | float    | 5.0           | The time after which the cameras will change. Only works if sequential_mode is selected |

### Class IPCameraManager
**Builder parameters (Same as USBCameraManager, but with an addition):**
| Parameter | Type      | Default | Description       |
| --------- | --------- | ------- | ----------------- |
| rtsp_urls | list[str] | []      | List of RTSP URLs |


## 🎨 Built With

<div align="center">

**Developed on**

<a href="https://github.com/meowrch">
<img src="assets/MeowrchBanner.png" alt="Meowrch Linux" width="300"/>
</a>

*[Meowrch](https://github.com/meowrch/meowrch) — A Linux distribution built for creators and developers*

</div>

## 🤝 Contributing

Contributions are welcome! Here's how you can help:

- 🐛 Report bugs and request features via [Issues](https://github.com/DIMFLIX/OmniView/issues)
- 🔧 Submit pull requests with improvements
- 📖 Improve documentation

## <a name="legal-status"></a>®️ Legal status
This project is protected by patent. All rights reserved. Use, copying, and distribution are permitted only with the written permission of the copyright holder.
| Page 1 | Page 2 |
|--------------------|--------------------|
| <img src="assets/1.png" width="300"> | <img src="assets/1_1.png" width="300"> |

## 📝 License

This project is licensed under the **GPL-3.0 License** - see the [LICENSE](LICENSE) file for details.