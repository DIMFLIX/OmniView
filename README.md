# Omniview
Система для одновременного просмотра и обработки потоков с нескольких камер (USB/IP) с возможностью интеграции в компьютерное зрение.

## 🚀 Возможности
- Поддержка USB и IP-камер (через RTSP)
- Автоматическое переподключение при обрыве связи
- Настраиваемые параметры камер (разрешение, FPS)
- Многопоточная обработка кадров
- Гибкая система обратных вызовов для обработки видео
- Готовый GUI для просмотра потоков
- Конфигурирование через параметры конструктора

## ⚙️ Установка
1. Клонируйте репозиторий:
```bash
git clone https://github.com/DIMFLIX/OmniView.git
cd OmniView
```

2. Установите [пакетный менеджер](https://docs.astral.sh/uv/getting-started/installation/)

3. Установите зависимости:
```bash
uv sync
```

4. Запустите базовый пример
```bash
python example.py
```

## 🛠️ Использование
### Базовый пример
```python
from omniview import CameraManager

def frame_callback(camera_id, frame):
    # Ваша обработка кадра
    pass

config = {
    'use_ip_cameras': True,
    'ip_cameras': [
        "rtsp://admin:password@192.168.1.100/stream1",
        "rtsp://user:pass@192.168.1.101:554/ch0.h264"
    ],
    'show_gui': True,
    'frame_callback': frame_callback
}

manager = CameraManager(**config)

try:
    manager.start()
except KeyboardInterrupt:
    manager.stop()
```

## 📚 API
### Класс CameraManager
**Основные методы:**
- `start()` - запускает менеджер камер (блокирующий вызов)
- `stop()` - корректно останавливает все потоки
- `process_frames()` - возвращает словарь текущих кадров (ID: кадр)

**Параметры конструктора:**
| Параметр         | Тип       | По умолчанию | Описание                     |
|------------------|-----------|--------------|------------------------------|
| use_ip_cameras   | bool      | False        | Использовать IP-камеры       |
| ip_cameras       | list[str] | []           | Список RTSP URL              |
| show_gui         | bool      | True         | Показывать окна с видео      |
| max_cameras      | int       | 10           | Макс. количество камер       |
| frame_width      | int       | 640          | Ширина кадра                 |
| frame_height     | int       | 480          | Высота кадра                 |
| fps              | int       | 30           | Целевой FPS                  |
| min_uptime       | float     | 5.0          | Мин. время работы (сек)      |
| frame_callback   | function  | None         | Callback для обработки кадров|
| exit_keys        | tuple     | (ord('q'),27)| Клавиши для выхода           |

## 🤝 Развитие проекта
Приветствуются:
- Отчеты об ошибках
- Pull requests
- Идеи по улучшению
- Примеры использования

## 📄 Лицензия
Проект распространяется под лицензией GNU GPL v3.
Подробности см. в файле [LICENSE](LICENSE).
