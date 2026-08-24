"""Общая настройка тестов.

Раньше здесь подменялись заглушками aiogram, OpenAI и Google Sheets — иначе
тесты не запускались без установленных зависимостей. Теперь окружение
собирается из requirements, и заглушки только мешали: клавиатуры и хендлеры
проверить было нельзя, вместо них приходил MagicMock.

Ничего сетевого при импорте не происходит: клиент OpenAI создаётся лениво,
Google Sheets читается только по вызову.
"""
import sys
from pathlib import Path

ROOT = str(Path(__file__).parent.parent)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
