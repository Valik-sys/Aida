from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import secrets
import string
from typing import Any, Dict, List, Optional, Sequence

import aiosqlite

import config
from database.models import ROLE_STUDENT, ROLE_TEACHER, Teacher, User


DB_PATH = config.HISTORY_CT_DB_PATH

# Без похожих символов: 0/O, 1/I/L — код диктуют голосом и переписывают руками
_INVITE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_INVITE_LENGTH = 6


def _utcnow() -> dt.datetime:
    # Наивный UTC: формат строк в БД остаётся прежним, но без DeprecationWarning
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def _now() -> str:
    return _utcnow().isoformat()


def _parse_dt(value: Optional[str]) -> Optional[dt.datetime]:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value)
    except ValueError:
        return None


async def init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                role TEXT,
                teacher_id INTEGER,
                current_subject TEXT,
                name TEXT NOT NULL DEFAULT '',
                class_name TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                last_active_at TEXT
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS teachers (
                tg_user_id INTEGER NOT NULL,
                subject TEXT NOT NULL,
                invite_code TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (tg_user_id, subject)
            )
            """
        )
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_teachers_invite ON teachers (invite_code)"
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS teacher_content (
                tg_user_id INTEGER NOT NULL,
                subject TEXT NOT NULL,
                kind TEXT NOT NULL,
                path TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'empty',
                items_count INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (tg_user_id, subject, kind)
            )
            """
        )
        # Настройки преподавателя. Ключ-значение осознанно: настроек будет
        # больше, и каждая новая не должна требовать правки схемы.
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS teacher_settings (
                tg_user_id INTEGER NOT NULL,
                subject TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (tg_user_id, subject, key)
            )
            """
        )
        # Журнал ответов — «чеки». Нужен, чтобы посмотреть недавнюю историю
        # и разобрать спорную ситуацию. Чистится по возрасту, см. prune_answer_log.
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS answer_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                teacher_id INTEGER,
                subject TEXT NOT NULL DEFAULT '',
                question_hash TEXT NOT NULL,
                is_correct INTEGER NOT NULL,
                student_answer TEXT NOT NULL DEFAULT '',
                answered_at TEXT NOT NULL
            )
            """
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_answer_log_user ON answer_log (user_id, answered_at)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_answer_log_age ON answer_log (answered_at)"
        )

        # Счётчики — «итог в тетради». Одна строка на пару «ученик и вопрос»,
        # не растут со временем. Хранятся всегда: восстановить их не из чего.
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS answer_stats (
                user_id INTEGER NOT NULL,
                question_hash TEXT NOT NULL,
                teacher_id INTEGER,
                subject TEXT NOT NULL DEFAULT '',
                question_preview TEXT NOT NULL DEFAULT '',
                attempts INTEGER NOT NULL DEFAULT 0,
                correct INTEGER NOT NULL DEFAULT 0,
                last_correct INTEGER NOT NULL DEFAULT 0,
                last_answered_at TEXT NOT NULL,
                streak INTEGER NOT NULL DEFAULT 0,
                due_at TEXT,
                PRIMARY KEY (user_id, question_hash)
            )
            """
        )
        # Таблица могла быть создана раньше, без расписания повторов.
        # Миграций в проекте нет, поэтому добавляем колонки поштучно.
        for column, definition in (
            ("streak", "INTEGER NOT NULL DEFAULT 0"),
            ("due_at", "TEXT"),
        ):
            try:
                await db.execute(f"ALTER TABLE answer_stats ADD COLUMN {column} {definition}")
            except Exception:  # noqa: BLE001
                pass
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_answer_stats_due ON answer_stats (user_id, due_at)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_answer_stats_teacher ON answer_stats (teacher_id)"
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS mistakes (
                user_id INTEGER NOT NULL,
                question_hash TEXT NOT NULL,
                question_json TEXT NOT NULL,
                tema_kod TEXT NOT NULL DEFAULT '',
                added_at TEXT NOT NULL,
                PRIMARY KEY (user_id, question_hash)
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS completed_topics (
                user_id INTEGER NOT NULL,
                section_num TEXT NOT NULL,
                tema_kod TEXT NOT NULL,
                completed_at TEXT NOT NULL,
                PRIMARY KEY (user_id, tema_kod)
            )
            """
        )
        # Жалобы учеников на вопросы. Ключ — тот же отпечаток вопроса, что
        # у ответов и ошибок: пересборка tests.json его не меняет.
        # Один ученик — одна жалоба на вопрос, повторная заменяет прежнюю.
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS question_reports (
                teacher_id INTEGER NOT NULL,
                subject TEXT NOT NULL DEFAULT '',
                question_hash TEXT NOT NULL,
                student_id INTEGER NOT NULL,
                reason TEXT NOT NULL,
                question_preview TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                PRIMARY KEY (teacher_id, question_hash, student_id)
            )
            """
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_question_reports_teacher "
            "ON question_reports (teacher_id, subject)"
        )
        # Решение по вопросу: скрыт автоматически, убран преподавателем
        # или подтверждён им как исправный.
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS question_flags (
                teacher_id INTEGER NOT NULL,
                subject TEXT NOT NULL DEFAULT '',
                question_hash TEXT NOT NULL,
                status TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (teacher_id, question_hash)
            )
            """
        )
        await db.commit()


# ---------- Пользователи ----------

_USER_FIELDS = (
    "telegram_id, role, teacher_id, current_subject, name, class_name, created_at, last_active_at"
)


def _row_to_user(row) -> User:
    return User(
        telegram_id=row[0],
        role=row[1],
        teacher_id=row[2],
        current_subject=row[3],
        name=row[4] or "",
        class_name=row[5] or "",
        created_at=_parse_dt(row[6]),
        last_active_at=_parse_dt(row[7]),
    )


async def get_user(telegram_id: int) -> Optional[User]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            f"SELECT {_USER_FIELDS} FROM users WHERE telegram_id = ?", (telegram_id,)
        ) as cursor:
            row = await cursor.fetchone()
    return _row_to_user(row) if row else None


async def ensure_user(telegram_id: int) -> User:
    """Создаёт пустую запись, если пользователя ещё нет. Роль пока не задана."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users (telegram_id, created_at) VALUES (?, ?)",
            (telegram_id, _now()),
        )
        await db.commit()
    user = await get_user(telegram_id)
    assert user is not None
    return user


async def set_role(telegram_id: int, role: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET role = ? WHERE telegram_id = ?", (role, telegram_id)
        )
        await db.commit()


async def set_subject(telegram_id: int, subject: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET current_subject = ? WHERE telegram_id = ?",
            (subject, telegram_id),
        )
        await db.commit()


async def set_profile(telegram_id: int, name: str, class_name: str = "") -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET name = ?, class_name = ? WHERE telegram_id = ?",
            (name, class_name, telegram_id),
        )
        await db.commit()


async def bind_student(student_id: int, teacher_id: int, subject: str) -> None:
    """Привязывает ученика к преподавателю. Предмет берётся от преподавателя.

    При переходе к другому преподавателю копилка ошибок очищается: там лежат
    вопросы прежнего преподавателя целиком, вместе с текстом и вариантами,
    и режим «Мои ошибки» показывал бы их новому ученику. Это ломало бы главный
    инвариант — ученик видит только материалы своего преподавателя.
    """
    previous = await get_user(student_id)
    teacher_changed = bool(
        previous and previous.teacher_id and previous.teacher_id != teacher_id
    )

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE users
            SET role = ?, teacher_id = ?, current_subject = ?
            WHERE telegram_id = ?
            """,
            (ROLE_STUDENT, teacher_id, subject, student_id),
        )
        if teacher_changed:
            await db.execute("DELETE FROM mistakes WHERE user_id = ?", (student_id,))
            await db.execute("DELETE FROM completed_topics WHERE user_id = ?", (student_id,))
        await db.commit()


async def update_last_active(telegram_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET last_active_at = ? WHERE telegram_id = ?",
            (_now(), telegram_id),
        )
        await db.commit()


async def get_inactive_users(days: int = 7) -> List[User]:
    """Ученики, которые не заходили `days` дней. Преподавателей не трогаем."""
    cutoff = (_utcnow() - dt.timedelta(days=days)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            f"""
            SELECT {_USER_FIELDS} FROM users
            WHERE role = ? AND last_active_at IS NOT NULL AND last_active_at < ?
            """,
            (ROLE_STUDENT, cutoff),
        ) as cursor:
            rows = await cursor.fetchall()
    return [_row_to_user(r) for r in rows]


# ---------- Преподаватели ----------

def _generate_invite_code() -> str:
    return "".join(secrets.choice(_INVITE_ALPHABET) for _ in range(_INVITE_LENGTH))


async def ensure_teacher(tg_user_id: int, subject: str) -> Teacher:
    """Регистрирует преподавателя по предмету и выдаёт код приглашения.

    Повторный вызов возвращает существующую запись — код не меняется,
    иначе разосланные ученикам ссылки перестали бы работать.
    """
    existing = await get_teacher(tg_user_id, subject)
    if existing:
        return existing

    async with aiosqlite.connect(DB_PATH) as db:
        for _ in range(10):
            code = _generate_invite_code()
            try:
                await db.execute(
                    """
                    INSERT INTO teachers (tg_user_id, subject, invite_code, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (tg_user_id, subject, code, _now()),
                )
                await db.commit()
                break
            except aiosqlite.IntegrityError:
                # Коллизия кода — пробуем другой
                continue
        else:
            raise RuntimeError("Не удалось выдать уникальный код приглашения")

    teacher = await get_teacher(tg_user_id, subject)
    assert teacher is not None
    return teacher


async def get_teacher(tg_user_id: int, subject: str) -> Optional[Teacher]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT tg_user_id, subject, invite_code, created_at
            FROM teachers WHERE tg_user_id = ? AND subject = ?
            """,
            (tg_user_id, subject),
        ) as cursor:
            row = await cursor.fetchone()
    if not row:
        return None
    return Teacher(tg_user_id=row[0], subject=row[1], invite_code=row[2], created_at=_parse_dt(row[3]))


async def get_teacher_by_invite(code: str) -> Optional[Teacher]:
    code = (code or "").strip().upper()
    if not code:
        return None
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT tg_user_id, subject, invite_code, created_at
            FROM teachers WHERE invite_code = ?
            """,
            (code,),
        ) as cursor:
            row = await cursor.fetchone()
    if not row:
        return None
    return Teacher(tg_user_id=row[0], subject=row[1], invite_code=row[2], created_at=_parse_dt(row[3]))


async def get_teacher_subjects(tg_user_id: int) -> List[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT subject FROM teachers WHERE tg_user_id = ? ORDER BY created_at",
            (tg_user_id,),
        ) as cursor:
            rows = await cursor.fetchall()
    return [r[0] for r in rows]


async def get_teacher_students(tg_user_id: int) -> List[User]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            f"SELECT {_USER_FIELDS} FROM users WHERE teacher_id = ? ORDER BY created_at",
            (tg_user_id,),
        ) as cursor:
            rows = await cursor.fetchall()
    return [_row_to_user(r) for r in rows]


async def count_completed_topics(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM completed_topics WHERE user_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
    return row[0] if row else 0


async def count_teacher_students(tg_user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM users WHERE teacher_id = ?", (tg_user_id,)
        ) as cursor:
            row = await cursor.fetchone()
    return row[0] if row else 0


# ---------- Настройки преподавателя ----------

async def get_teacher_setting(
    tg_user_id: int, subject: str, key: str, default: Optional[str] = None
) -> Optional[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT value FROM teacher_settings
            WHERE tg_user_id = ? AND subject = ? AND key = ?
            """,
            (tg_user_id, subject, key),
        ) as cursor:
            row = await cursor.fetchone()
    return row[0] if row else default


async def set_teacher_setting(tg_user_id: int, subject: str, key: str, value: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO teacher_settings (tg_user_id, subject, key, value, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (tg_user_id, subject, key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (tg_user_id, subject, key, value, _now()),
        )
        await db.commit()


# ---------- Контент преподавателя ----------

async def set_teacher_content(
    tg_user_id: int,
    subject: str,
    kind: str,
    path: str,
    items_count: int,
    status: str = "ready",
) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO teacher_content (tg_user_id, subject, kind, path, status, items_count, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (tg_user_id, subject, kind) DO UPDATE SET
                path = excluded.path,
                status = excluded.status,
                items_count = excluded.items_count,
                updated_at = excluded.updated_at
            """,
            (tg_user_id, subject, kind, path, status, items_count, _now()),
        )
        await db.commit()


async def get_teacher_content(tg_user_id: int, subject: str, kind: str) -> Optional[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT path, status, items_count, updated_at
            FROM teacher_content WHERE tg_user_id = ? AND subject = ? AND kind = ?
            """,
            (tg_user_id, subject, kind),
        ) as cursor:
            row = await cursor.fetchone()
    if not row:
        return None
    return {"path": row[0], "status": row[1], "items_count": row[2], "updated_at": row[3]}


# ---------- Ответы: журнал и счётчики ----------

PREVIEW_LEN = 120
ANSWER_LOG_KEEP_DAYS = 90

# Интервалы повторения в днях по числу верных ответов подряд.
# Ошибся — счётчик сбрасывается, и вопрос возвращается сразу.
REPEAT_INTERVALS = [1, 3, 7, 14, 30, 60]


def question_hash(question_text: str) -> str:
    """Отпечаток вопроса — по нему связаны ответы, ошибки и расписание."""
    return _question_hash(question_text)


def _next_due(streak: int, answered_at: dt.datetime) -> str:
    """Когда показать вопрос снова."""
    if streak <= 0:
        return answered_at.isoformat()
    days = REPEAT_INTERVALS[min(streak, len(REPEAT_INTERVALS)) - 1]
    return (answered_at + dt.timedelta(days=days)).isoformat()


async def record_answer(
    user_id: int,
    question_text: str,
    is_correct: bool,
    *,
    teacher_id: Optional[int] = None,
    subject: str = "",
    student_answer: str = "",
) -> None:
    """Записывает ответ в журнал и обновляет счётчики.

    Счётчики обновляются здесь же и намеренно: досчитать их задним числом
    нельзя — журнал к тому времени будет вычищен по возрасту.
    """
    qhash = _question_hash(question_text)
    answered_at = _utcnow()
    now = answered_at.isoformat()
    correct_flag = 1 if is_correct else 0

    async with aiosqlite.connect(DB_PATH) as db:
        # Серия верных ответов подряд задаёт следующий интервал повторения
        async with db.execute(
            "SELECT streak FROM answer_stats WHERE user_id = ? AND question_hash = ?",
            (user_id, qhash),
        ) as cursor:
            row = await cursor.fetchone()
        streak = (row[0] if row else 0) + 1 if is_correct else 0
        due_at = _next_due(streak, answered_at)

        await db.execute(
            """
            INSERT INTO answer_log
                (user_id, teacher_id, subject, question_hash, is_correct, student_answer, answered_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id, teacher_id, subject, qhash, correct_flag,
                (student_answer or "")[:PREVIEW_LEN], now,
            ),
        )
        await db.execute(
            """
            INSERT INTO answer_stats
                (user_id, question_hash, teacher_id, subject, question_preview,
                 attempts, correct, last_correct, last_answered_at, streak, due_at)
            VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
            ON CONFLICT (user_id, question_hash) DO UPDATE SET
                attempts = attempts + 1,
                correct = correct + excluded.correct,
                last_correct = excluded.last_correct,
                last_answered_at = excluded.last_answered_at,
                streak = excluded.streak,
                due_at = excluded.due_at,
                teacher_id = excluded.teacher_id,
                subject = excluded.subject
            """,
            (
                user_id, qhash, teacher_id, subject,
                (question_text or "").strip()[:PREVIEW_LEN],
                correct_flag, correct_flag, now, streak, due_at,
            ),
        )
        await db.commit()


async def get_schedule(user_id: int) -> Dict[str, Dict[str, Any]]:
    """Расписание по всем вопросам ученика: отпечаток → когда повторить.

    Отдаём одним запросом: вызывается при наборе теста, и ходить в базу
    за каждым вопросом было бы расточительно.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT question_hash, streak, due_at, last_correct FROM answer_stats "
            "WHERE user_id = ?",
            (user_id,),
        ) as cursor:
            rows = await cursor.fetchall()

    return {
        r[0]: {"streak": r[1] or 0, "due_at": r[2], "last_correct": bool(r[3])}
        for r in rows
    }


async def count_due(
    user_id: int,
    teacher_id: Optional[int] = None,
    subject: str = "",
) -> int:
    """Сколько вопросов пора повторить прямо сейчас.

    Считаем только по текущему набору: после перехода к другому преподавателю
    старые вопросы ученику всё равно не попадутся, и обещать их в счётчике
    было бы враньём.

    Пустой срок (строки, созданные до появления расписания) считается
    просроченным — так же, как при наборе теста.
    """
    params: List[Any] = [user_id, _now()]
    where = ["user_id = ?", "(due_at IS NULL OR due_at <= ?)"]
    if teacher_id is not None:
        where.append("teacher_id = ?")
        params.append(teacher_id)
    if subject:
        where.append("subject = ?")
        params.append(subject)

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            f"SELECT COUNT(*) FROM answer_stats WHERE {' AND '.join(where)}", params
        ) as cursor:
            row = await cursor.fetchone()
    return row[0] if row else 0


async def get_activity_streak(user_id: int) -> int:
    """Сколько дней подряд ученик занимался, считая от сегодня или вчера.

    Считается по журналу, поэтому серия длиннее срока его хранения
    не покажется — для мотивации этого с запасом.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT DISTINCT date(answered_at) FROM answer_log "
            "WHERE user_id = ? ORDER BY 1 DESC",
            (user_id,),
        ) as cursor:
            rows = await cursor.fetchall()

    days = [dt.date.fromisoformat(r[0]) for r in rows if r[0]]
    if not days:
        return 0

    # Вчерашняя серия ещё жива: сегодняшний день не закончился
    if (_utcnow().date() - days[0]).days > 1:
        return 0

    streak = 1
    for newer, older in zip(days, days[1:]):
        if (newer - older).days != 1:
            break
        streak += 1
    return streak


async def get_accuracy_window(
    user_id: int, days_ago_from: int, days_ago_to: int = 0
) -> Optional[int]:
    """Точность за отрезок времени. None, если ответов в нём не было.

    Нужна, чтобы показывать движение: общий процент за всё время меняется
    слишком медленно и никого не мотивирует.
    """
    now = _utcnow()
    start = (now - dt.timedelta(days=days_ago_from)).isoformat()
    end = (now - dt.timedelta(days=days_ago_to)).isoformat()

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*), COALESCE(SUM(is_correct), 0) FROM answer_log "
            "WHERE user_id = ? AND answered_at >= ? AND answered_at < ?",
            (user_id, start, end),
        ) as cursor:
            row = await cursor.fetchone()

    total, correct = row or (0, 0)
    return round(100 * correct / total) if total else None


async def get_students_stats(teacher_id: int, subject: str = "") -> List[Dict[str, Any]]:
    """Сводка по каждому ученику: сколько отвечал и с какой точностью.

    Считаются только ответы, данные этому преподавателю: средний по классу
    (get_teacher_totals) фильтруется так же, и без этого ученик, сменивший
    преподавателя, сравнивался бы с классом по чужой статистике.
    """
    # Предмет фильтруем в самом соединении, иначе ученики без ответов
    # выпали бы из списка вместе с пустыми строками статистики
    subject_filter = "AND s.subject = ?" if subject else ""
    params: List[Any] = [teacher_id] + ([subject] if subject else []) + [teacher_id]

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            f"""
            SELECT u.telegram_id, u.name,
                   COALESCE(SUM(s.attempts), 0), COALESCE(SUM(s.correct), 0)
            FROM users u
            LEFT JOIN answer_stats s
                   ON s.user_id = u.telegram_id
                  AND s.teacher_id = ? {subject_filter}
            WHERE u.teacher_id = ?
            GROUP BY u.telegram_id, u.name
            """,
            params,
        ) as cursor:
            rows = await cursor.fetchall()

    out: List[Dict[str, Any]] = []
    for telegram_id, name, attempts, correct in rows:
        attempts = attempts or 0
        out.append({
            "telegram_id": telegram_id,
            "name": name or "",
            "attempts": attempts,
            "accuracy": round(100 * (correct or 0) / attempts) if attempts else None,
        })
    return out


async def get_student_progress(user_id: int) -> Dict[str, Any]:
    """Сводка по ученику: сколько вопросов повидал и как отвечает."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT COUNT(*), COALESCE(SUM(attempts), 0), COALESCE(SUM(correct), 0),
                   COALESCE(SUM(last_correct), 0), MAX(last_answered_at)
            FROM answer_stats WHERE user_id = ?
            """,
            (user_id,),
        ) as cursor:
            row = await cursor.fetchone()

    questions, attempts, correct, mastered, last_at = row or (0, 0, 0, 0, None)
    return {
        "questions": questions or 0,
        "attempts": attempts or 0,
        "correct": correct or 0,
        # Вопросы, которые в последний раз были отвечены верно
        "mastered": mastered or 0,
        "accuracy": round(100 * correct / attempts) if attempts else 0,
        "last_answered_at": _parse_dt(last_at),
    }


async def get_hard_questions(
    teacher_id: int, limit: int = 5, subject: str = "", min_errors: int = 3
) -> List[Dict[str, Any]]:
    """Вопросы, на которых чаще всего ошибаются ученики этого преподавателя.

    Верхние строки — первые кандидаты на проверку: обычно там опечатка
    в ключе, а не сложная тема.

    Собственные ответы преподавателя не считаются: он проходит свой тренажёр
    в режиме ученика, и его попытки выдавались бы за работу класса.

    Порог по числу ошибок нужен, чтобы список не заполнялся строками
    «1 из 1» — единственная ошибка ничего не говорит ни о вопросе,
    ни о классе.
    """
    params: List[Any] = [teacher_id, teacher_id]
    subject_filter = ""
    if subject:
        subject_filter = "AND subject = ?"
        params.append(subject)
    params.extend([min_errors, limit])

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            f"""
            SELECT question_hash,
                   MAX(question_preview),
                   SUM(attempts) AS total,
                   SUM(attempts) - SUM(correct) AS errors,
                   COUNT(DISTINCT user_id) AS students
            FROM answer_stats
            WHERE teacher_id = ? AND user_id != ? {subject_filter}
            GROUP BY question_hash
            HAVING errors >= ?
            ORDER BY errors DESC, total DESC
            LIMIT ?
            """,
            params,
        ) as cursor:
            rows = await cursor.fetchall()

    return [
        {
            "question_hash": r[0], "preview": r[1], "attempts": r[2],
            "errors": r[3], "students": r[4],
            "error_rate": round(100 * r[3] / r[2]) if r[2] else 0,
        }
        for r in rows
    ]


async def get_teacher_totals(teacher_id: int, subject: str = "") -> Dict[str, int]:
    """Сколько всего ответов дали ученики этого преподавателя.

    Ответы самого преподавателя не в счёт — см. get_hard_questions.
    """
    params: List[Any] = [teacher_id, teacher_id]
    subject_filter = ""
    if subject:
        subject_filter = "AND subject = ?"
        params.append(subject)

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            f"""
            SELECT COALESCE(SUM(attempts), 0), COALESCE(SUM(correct), 0),
                   COUNT(DISTINCT user_id)
            FROM answer_stats
            WHERE teacher_id = ? AND user_id != ? {subject_filter}
            """,
            params,
        ) as cursor:
            row = await cursor.fetchone()

    attempts, correct, students = row or (0, 0, 0)
    return {
        "attempts": attempts or 0,
        "correct": correct or 0,
        "students": students or 0,
        "accuracy": round(100 * correct / attempts) if attempts else 0,
    }


async def prune_answer_log(days: int = ANSWER_LOG_KEEP_DAYS) -> int:
    """Удаляет старые записи журнала. Счётчики не трогает."""
    cutoff = (_utcnow() - dt.timedelta(days=days)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("DELETE FROM answer_log WHERE answered_at < ?", (cutoff,))
        await db.commit()
        return cursor.rowcount or 0


# ---------- Ошибки ----------

def _question_hash(question_text: str) -> str:
    return hashlib.md5(question_text.encode()).hexdigest()[:16]


async def add_mistake(user_id: int, question: Dict[str, Any], tema_kod: str) -> None:
    qhash = _question_hash(question.get("question_text", ""))
    question_with_hash = {**question, "question_hash": qhash}
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT OR IGNORE INTO mistakes (user_id, question_hash, question_json, tema_kod, added_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, qhash, json.dumps(question_with_hash, ensure_ascii=False), tema_kod, _now()),
        )
        await db.commit()


async def remove_mistake(user_id: int, question_hash: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM mistakes WHERE user_id = ? AND question_hash = ?",
            (user_id, question_hash),
        )
        await db.commit()


async def get_mistakes(user_id: int) -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT question_json FROM mistakes WHERE user_id = ? ORDER BY added_at",
            (user_id,),
        ) as cursor:
            rows = await cursor.fetchall()
    questions = [json.loads(row[0]) for row in rows]
    for q in questions:
        if q.get("options"):
            q["options"] = {int(k): v for k, v in q["options"].items()}
    return questions


async def count_mistakes(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM mistakes WHERE user_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
    return row[0] if row else 0


# ---------- Пройденные темы ----------

async def mark_topic_completed(user_id: int, section_num: str, tema_kod: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT OR IGNORE INTO completed_topics (user_id, section_num, tema_kod, completed_at)
            VALUES (?, ?, ?, ?)
            """,
            (user_id, section_num, tema_kod, _now()),
        )
        await db.commit()


async def get_completed_topics(user_id: int, section_num: str) -> set:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT tema_kod FROM completed_topics WHERE user_id = ? AND section_num = ?",
            (user_id, section_num),
        ) as cursor:
            rows = await cursor.fetchall()
    return {row[0] for row in rows}


# ---------- Жалобы на вопросы ----------

async def add_question_report(
    teacher_id: int,
    subject: str,
    question_hash: str,
    student_id: int,
    reason: str,
    question_preview: str = "",
) -> List[str]:
    """Записывает жалобу и возвращает все причины по этому вопросу.

    Повторная жалоба того же ученика заменяет прежнюю: передумал —
    значит теперь причина другая, а не две сразу.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO question_reports
                (teacher_id, subject, question_hash, student_id, reason,
                 question_preview, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(teacher_id, question_hash, student_id) DO UPDATE SET
                reason = excluded.reason,
                subject = excluded.subject,
                question_preview = excluded.question_preview,
                created_at = excluded.created_at
            """,
            (teacher_id, subject, question_hash, student_id, reason,
             question_preview[:200], _now()),
        )
        await db.commit()
        async with db.execute(
            "SELECT reason FROM question_reports WHERE teacher_id = ? AND question_hash = ?",
            (teacher_id, question_hash),
        ) as cursor:
            rows = await cursor.fetchall()
    return [row[0] for row in rows]


async def get_question_status(teacher_id: int, question_hash: str) -> Optional[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT status FROM question_flags WHERE teacher_id = ? AND question_hash = ?",
            (teacher_id, question_hash),
        ) as cursor:
            row = await cursor.fetchone()
    return row[0] if row else None


async def set_question_status(
    teacher_id: int, subject: str, question_hash: str, status: str
) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO question_flags
                (teacher_id, subject, question_hash, status, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(teacher_id, question_hash) DO UPDATE SET
                status = excluded.status,
                subject = excluded.subject,
                updated_at = excluded.updated_at
            """,
            (teacher_id, subject, question_hash, status, _now()),
        )
        await db.commit()


async def clear_question_reports(teacher_id: int, question_hash: str) -> None:
    """Убирает жалобы после решения преподавателя — вопрос уходит из списка."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM question_reports WHERE teacher_id = ? AND question_hash = ?",
            (teacher_id, question_hash),
        )
        await db.commit()


async def get_hidden_questions(teacher_id: int, statuses: Sequence[str]) -> set:
    """Отпечатки вопросов, которых ученикам показывать нельзя."""
    if not statuses:
        return set()
    placeholders = ",".join("?" for _ in statuses)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            f"SELECT question_hash FROM question_flags "
            f"WHERE teacher_id = ? AND status IN ({placeholders})",
            (teacher_id, *statuses),
        ) as cursor:
            rows = await cursor.fetchall()
    return {row[0] for row in rows}


async def get_student_reported(student_id: int, reasons: Sequence[str]) -> set:
    """На что этот ученик пожаловался — чтобы не показывать ему это снова."""
    if not reasons:
        return set()
    placeholders = ",".join("?" for _ in reasons)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            f"SELECT question_hash FROM question_reports "
            f"WHERE student_id = ? AND reason IN ({placeholders})",
            (student_id, *reasons),
        ) as cursor:
            rows = await cursor.fetchall()
    return {row[0] for row in rows}


async def get_question_reports(teacher_id: int, subject: str) -> List[Dict[str, Any]]:
    """Вопросы с незакрытыми жалобами — по одному словарю на вопрос.

    Имя ученика берём из профиля: преподавателю важно, кто именно
    споткнулся, а не безличное «отметили двое».
    """
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT r.question_hash, r.reason, r.student_id, r.question_preview,
                   r.created_at, COALESCE(u.name, '') AS student_name
            FROM question_reports r
            LEFT JOIN users u ON u.telegram_id = r.student_id
            WHERE r.teacher_id = ? AND r.subject = ?
            ORDER BY r.created_at
            """,
            (teacher_id, subject),
        ) as cursor:
            rows = await cursor.fetchall()

        async with db.execute(
            "SELECT question_hash, status FROM question_flags WHERE teacher_id = ?",
            (teacher_id,),
        ) as cursor:
            statuses = {r[0]: r[1] for r in await cursor.fetchall()}

        # Сколько учеников отвечали на вопрос и сколько ошибались — по этому
        # преподаватель за секунду отличает брак от «не поняли»
        async with db.execute(
            """
            SELECT question_hash, COUNT(*),
                   SUM(CASE WHEN correct < attempts THEN 1 ELSE 0 END)
            FROM answer_stats
            WHERE teacher_id = ? AND subject = ?
            GROUP BY question_hash
            """,
            (teacher_id, subject),
        ) as cursor:
            stats = {r[0]: (r[1] or 0, r[2] or 0) for r in await cursor.fetchall()}

    grouped: Dict[str, Dict[str, Any]] = {}
    for qhash, reason, student_id, preview, created_at, name in rows:
        item = grouped.setdefault(qhash, {
            "question_hash": qhash,
            "preview": preview or "",
            "reasons": [],
            "students": [],
            "created_at": created_at,
            "status": statuses.get(qhash),
        })
        item["reasons"].append(reason)
        item["students"].append(name or f"Ученик {student_id}")

    for qhash, item in grouped.items():
        answered, wrong = stats.get(qhash, (0, 0))
        item["answered_by"] = answered
        item["wrong_by"] = wrong

    return list(grouped.values())
