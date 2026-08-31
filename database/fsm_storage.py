"""Хранилище состояний бота на SQLite: сессия переживает перезапуск.

Зачем: в `MemoryStorage` незаконченный тест живёт в оперативной памяти
процесса. Любой перезапуск — обновление кода, падение, перезагрузка
сервера — обрывает всем ученикам тесты и карточки посреди работы, молча
и без всякого сообщения. Ученик решает, что бот сломался.

Почему SQLite, а не Redis: база у нас уже есть, отдельная служба на сервере
не нужна, а нагрузка тут — одна запись на нажатие кнопки. Redis имеет смысл,
когда ботов несколько или процессов больше одного; сейчас ни того, ни другого.

Таблица своя, к остальной схеме отношения не имеет: состояние — это не данные
о людях, оно живёт часами и его не жалко потерять при чистке.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
from typing import Any, Dict, Mapping, Optional

import aiosqlite
from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StorageKey

import config


logger = logging.getLogger(__name__)


# Сколько живёт брошенная сессия. Незаконченный тест имеет смысл вернуть
# на следующий день, но не через месяц: за это время преподаватель мог
# перезалить материалы, и вопросы в сессии уже не те.
SESSION_TTL_HOURS = 48


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


class SQLiteStorage(BaseStorage):
    """Состояние и данные FSM в таблице `fsm_sessions`."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self._db_path = db_path or config.HISTORY_CT_DB_PATH
        self._ready = False

    # ---------- служебное ----------

    def _key(self, key: StorageKey) -> str:
        """Строковый ключ сессии.

        Складываем все поля StorageKey, а не только пользователя: один и тот
        же человек может вести отдельные сессии в разных чатах, а `destiny`
        разделяет параллельные сценарии внутри одного чата.
        """
        return ":".join(
            str(part) for part in (
                key.bot_id, key.chat_id, key.user_id,
                key.thread_id or 0, key.business_connection_id or "",
                key.destiny,
            )
        )

    def _open(self):
        """Соединение на одну операцию.

        Возвращаем сам менеджер контекста, а не готовое соединение:
        в aiosqlite вход в `async with` сам дожидается подключения, и если
        соединение уже дождались раньше, второе ожидание не возвращается —
        бот повисает намертво.
        """
        os.makedirs(os.path.dirname(self._db_path) or ".", exist_ok=True)
        return aiosqlite.connect(self._db_path)

    async def _ensure(self, db: aiosqlite.Connection) -> None:
        if self._ready:
            return
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS fsm_sessions (
                key TEXT PRIMARY KEY,
                state TEXT,
                data TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL
            )
            """
        )
        await db.commit()
        self._ready = True

    async def _row(self, db: aiosqlite.Connection, key: str) -> tuple[Optional[str], Dict[str, Any]]:
        async with db.execute(
            "SELECT state, data FROM fsm_sessions WHERE key = ?", (key,)
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return None, {}
        return row[0], _loads(row[1], key)

    async def _write(
        self,
        db: aiosqlite.Connection,
        key: str,
        state: Optional[str],
        data: Dict[str, Any],
    ) -> None:
        await db.execute(
            """
            INSERT INTO fsm_sessions (key, state, data, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                state = excluded.state,
                data = excluded.data,
                updated_at = excluded.updated_at
            """,
            (key, state, json.dumps(data, ensure_ascii=False), _utcnow().isoformat()),
        )
        await db.commit()

    # ---------- интерфейс aiogram ----------

    async def set_state(self, key: StorageKey, state: Any = None) -> None:
        name = state.state if isinstance(state, State) else state
        skey = self._key(key)
        async with self._open() as db:
            await self._ensure(db)
            _old, data = await self._row(db, skey)
            # Сброс состояния при пустых данных — сессия закончилась, строку
            # не держим: иначе таблица растёт на каждое нажатие «в меню»
            if name is None and not data:
                await db.execute("DELETE FROM fsm_sessions WHERE key = ?", (skey,))
                await db.commit()
                return
            await self._write(db, skey, name, data)

    async def get_state(self, key: StorageKey) -> Optional[str]:
        async with self._open() as db:
            await self._ensure(db)
            state, _data = await self._row(db, self._key(key))
            return state

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        skey = self._key(key)
        async with self._open() as db:
            await self._ensure(db)
            state, _old = await self._row(db, skey)
            if state is None and not data:
                await db.execute("DELETE FROM fsm_sessions WHERE key = ?", (skey,))
                await db.commit()
                return
            await self._write(db, skey, state, dict(data))

    async def get_data(self, key: StorageKey) -> Dict[str, Any]:
        async with self._open() as db:
            await self._ensure(db)
            _state, data = await self._row(db, self._key(key))
            return data

    async def update_data(self, key: StorageKey, data: Mapping[str, Any]) -> Dict[str, Any]:
        """Дописывает поля к уже сохранённым.

        Своя реализация вместо унаследованной: та делает чтение и запись
        двумя отдельными обращениями, и два быстрых нажатия подряд могут
        затереть друг друга. Здесь всё внутри одного соединения.
        """
        skey = self._key(key)
        async with self._open() as db:
            await self._ensure(db)
            state, current = await self._row(db, skey)
            current.update(data)
            await self._write(db, skey, state, current)
            return current.copy()

    async def close(self) -> None:
        # Соединение открывается на операцию и закрывается сразу — держать
        # нечего. Метод нужен, потому что его требует интерфейс aiogram.
        return None

    # ---------- уборка ----------

    async def prune(self, hours: int = SESSION_TTL_HOURS) -> int:
        """Удаляет сессии, которых никто не касался дольше указанного срока."""
        cutoff = (_utcnow() - dt.timedelta(hours=hours)).isoformat()
        async with self._open() as db:
            await self._ensure(db)
            cursor = await db.execute(
                "DELETE FROM fsm_sessions WHERE updated_at <= ?", (cutoff,)
            )
            await db.commit()
            return cursor.rowcount or 0


def _loads(raw: str, key: str) -> Dict[str, Any]:
    """Данные сессии из JSON. Битую строку не роняем, а начинаем заново.

    Упасть здесь — значит уронить обработку сообщения и оставить ученика
    без ответа. Потерять недописанную сессию неприятно, но не смертельно.
    """
    try:
        value = json.loads(raw or "{}")
    except Exception:  # noqa: BLE001
        logger.warning("Битые данные сессии %s — начинаем с чистого листа", key)
        return {}
    return value if isinstance(value, dict) else {}
