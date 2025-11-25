
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
		<img src="https://img.shields.io/badge/README-RU-blue?color=cba6f7&labelColor=cba6f7&style=for-the-badge">
	</a>
	<a href="./README.md">
		<img src="https://img.shields.io/badge/README-ENG-blue?color=C9CBFF&labelColor=1C2325&style=for-the-badge">
	</a>
    <br>
    <br>

---

[О проекте](#about-project) • [Установка](#installation) • [Использование](#usage) • [API](#api) • [Правовой статус](#legal-status)


<br>
</div>


# <a name="about-project"></a>📝 О проекте
Система для одновременного просмотра и обработки потоков с нескольких камер (USB/IP) с возможностью интеграции в компьютерное зрение.

## 🚀 Возможности
- Поддержка USB и IP-камер (через RTSP)
- Автоматическое переподключение при обрыве связи
- Настраиваемые параметры камер (разрешение, FPS)
- Многопоточная обработка кадров
- Гибкая система обратных вызовов для обработки видео
- Готовый GUI для просмотра потоков
- Конфигурирование через параметры конструктора

## <a name="installation"></a>⚙️ Установка
```bash
pip install omniview
```

## <a name="usage"></a>🛠️ Использование
### Базовый пример для USB камер
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

### Базовый пример для IP камер
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

## <a name="api"></a>📚 API
**Основные методы:**
- `start()` - запускает менеджер камер (блокирующий вызов)
- `stop()` - корректно останавливает все потоки
- `process_frames()` - возвращает словарь текущих кадров (ID: кадр)

### 🔌 Класс USBCameraManager
**Параметры конструктора:**
| Параметр         | Тип       | По умолчанию | Описание                     |
|------------------|-----------|--------------|------------------------------|
| show_gui         | bool      | True         | Показывать окна с видео      |
| max_cameras      | int       | 10           | Макс. количество камер       |
| frame_width      | int       | 640          | Ширина кадра                 |
| frame_height     | int       | 480          | Высота кадра                 |
| fps              | int       | 30           | Целевой FPS                  |
| min_uptime       | float     | 5.0          | Мин. время работы (сек)      |
| frame_callback   | function  | None         | Callback для обработки кадров|
| exit_keys        | tuple     | (ord('q'),27)| Клавиши для выхода           |

### 🌐 Класс IPCameraManager
**Параметры конструктора (Все те-же самые что у USBCameraManager, но с добавлением):**
| Параметр         | Тип       | По умолчанию | Описание                     |
|------------------|-----------|--------------|------------------------------|
| rtsp_urls        | list[str] | []           | Список RTSP URL              |


## 🎨 Разработано с использованием

<div align="center">

**Разработано на**

<a href="https://github.com/meowrch">
<img src="assets/MeowrchBanner.png" alt="Meowrch Linux" width="300"/>
</a>

*[Meowrch](https://github.com/meowrch/meowrch) — Linux-дистрибутив, созданный для творцов и разработчиков*

</div>


## 🤝 Внести вклад

Мы приветствуем ваш вклад! Вот как вы можете помочь:

- 🐛 Сообщайте об ошибках и запрашивайте функции через [Issues](https://github.com/DIMFLIX/OmniView/issues)
- 🔧 Отправляйте pull requests с улучшениями
- 📖 Улучшайте документацию

## <a name="legal-status"></a>®️ Правовой статус
Данный проект защищён патентом. Все права защищены. Использование, копирование и распространение возможны только с письменного разрешения правообладателя.
| Страница 1 | Страница 2 |
|--------------------|--------------------|
| <img src="assets/1.png" width="300"> | <img src="assets/1_1.png" width="300"> |

## 📝 Лицензия

Этот проект лицензирован под **GPL-3.0 License** — см. файл [LICENSE](LICENSE) для деталей.