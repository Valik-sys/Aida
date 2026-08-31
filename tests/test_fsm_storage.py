"""Состояние бота на диске: сессия переживает перезапуск.

Главное, что здесь проверяется: ученик, начавший тест, после перезапуска
бота продолжает с того же места. В `MemoryStorage` сессия жила в памяти
процесса, и любое обновление кода обрывало тесты всем сразу — молча,
без сообщения об ошибке.
"""

import shutil
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aiogram.fsm.state import State, StatesGroup  # noqa: E402
from aiogram.fsm.storage.base import StorageKey  # noqa: E402

from database.fsm_storage import SQLiteStorage  # noqa: E402


class Flow(StatesGroup):
    waiting = State()


@pytest.fixture
def db_path():
    tmp = Path(tempfile.mkdtemp(prefix="aida_fsm_"))
    try:
        yield str(tmp / "test.sqlite3")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture
def storage(db_path):
    return SQLiteStorage(db_path)


KEY = StorageKey(bot_id=1, chat_id=10, user_id=10)
OTHER = StorageKey(bot_id=1, chat_id=20, user_id=20)


class TestBasics:
    async def test_empty_by_default(self, storage):
        assert await storage.get_state(KEY) is None
        assert await storage.get_data(KEY) == {}

    async def test_state_is_stored(self, storage):
        await storage.set_state(KEY, Flow.waiting)
        assert await storage.get_state(KEY) == Flow.waiting.state

    async def test_state_accepts_plain_string(self, storage):
        await storage.set_state(KEY, "Flow:waiting")
        assert await storage.get_state(KEY) == "Flow:waiting"

    async def test_data_is_stored(self, storage):
        await storage.set_data(KEY, {"idx": 3, "wrong": [1, 2]})
        assert await storage.get_data(KEY) == {"idx": 3, "wrong": [1, 2]}

    async def test_update_adds_to_existing(self, storage):
        await storage.set_data(KEY, {"idx": 3})
        result = await storage.update_data(KEY, {"section": "6_1"})

        assert result == {"idx": 3, "section": "6_1"}
        assert await storage.get_data(KEY) == {"idx": 3, "section": "6_1"}

    async def test_state_and_data_do_not_erase_each_other(self, storage):
        """Запись одного не должна стирать другое — на этом держится весь тест."""
        await storage.set_state(KEY, Flow.waiting)
        await storage.set_data(KEY, {"idx": 7})

        assert await storage.get_state(KEY) == Flow.waiting.state
        assert await storage.get_data(KEY) == {"idx": 7}

        await storage.set_state(KEY, None)
        assert await storage.get_data(KEY) == {"idx": 7}

    async def test_users_do_not_see_each_other(self, storage):
        await storage.set_data(KEY, {"idx": 1})
        await storage.set_data(OTHER, {"idx": 99})

        assert await storage.get_data(KEY) == {"idx": 1}
        assert await storage.get_data(OTHER) == {"idx": 99}

    async def test_cyrillic_survives(self, storage):
        """В сессии лежат тексты вопросов, а они по-русски."""
        await storage.set_data(KEY, {"вопрос": "Кто основал город?"})
        assert await storage.get_data(KEY) == {"вопрос": "Кто основал город?"}


class TestRestart:
    """То, ради чего всё затевалось."""

    async def test_session_survives_restart(self, db_path):
        before = SQLiteStorage(db_path)
        await before.set_state(KEY, Flow.waiting)
        await before.set_data(KEY, {
            "test_session": {"questions": [{"question_text": "Кто?"}], "idx": 5},
        })
        await before.close()

        # Бот перезапустили: новый процесс, новый объект хранилища
        after = SQLiteStorage(db_path)

        assert await after.get_state(KEY) == Flow.waiting.state
        assert (await after.get_data(KEY))["test_session"]["idx"] == 5

    async def test_finished_session_leaves_nothing(self, storage):
        """Иначе таблица копит строки на каждое нажатие «в меню»."""
        await storage.set_state(KEY, Flow.waiting)
        await storage.set_data(KEY, {"idx": 1})

        await storage.set_data(KEY, {})
        await storage.set_state(KEY, None)

        assert await storage.get_state(KEY) is None
        assert await storage.get_data(KEY) == {}


class TestHousekeeping:
    async def test_old_sessions_are_pruned(self, storage):
        await storage.set_data(KEY, {"idx": 1})

        assert await storage.prune(hours=0) == 1
        assert await storage.get_data(KEY) == {}

    async def test_fresh_sessions_are_kept(self, storage):
        await storage.set_data(KEY, {"idx": 1})

        assert await storage.prune(hours=48) == 0
        assert await storage.get_data(KEY) == {"idx": 1}

    async def test_broken_data_does_not_crash(self, storage, db_path):
        """Битую строку лучше потерять, чем уронить обработку сообщения."""
        import aiosqlite

        await storage.set_data(KEY, {"idx": 1})
        async with aiosqlite.connect(db_path) as db:
            await db.execute("UPDATE fsm_sessions SET data = '{не json'")
            await db.commit()

        assert await storage.get_data(KEY) == {}
