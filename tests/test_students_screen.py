"""Экран учеников: сводка, карточка ученика, доступ к чужим."""

import datetime as dt
import shutil
import sys
import tempfile
from pathlib import Path

import pytest_asyncio

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import database.db as db_module  # noqa: E402
from bot.handlers.menu import _humanize_date, _is_active, _utcnow  # noqa: E402
from bot.keyboards.inline import (  # noqa: E402
    STUDENTS_PAGE_SIZE,
    student_card_kb,
    students_kb,
)
from database.models import ROLE_TEACHER, User  # noqa: E402
from services import storage  # noqa: E402

TEACHER_A, TEACHER_B = 9801, 9802
STUDENT_1, STUDENT_2, STUDENT_OTHER = 9901, 9902, 9903
SUBJECT = "history"


@pytest_asyncio.fixture
async def env():
    tmpdir = Path(tempfile.mkdtemp(prefix="aida_stud_"))
    saved = (db_module.DB_PATH, storage.DATA_ROOT, storage.TEACHERS_ROOT)
    db_module.DB_PATH = str(tmpdir / "test.sqlite3")
    storage.DATA_ROOT = tmpdir / "data"
    storage.TEACHERS_ROOT = storage.DATA_ROOT / "teachers"
    try:
        await db_module.init_db()
        yield tmpdir
    finally:
        (db_module.DB_PATH, storage.DATA_ROOT, storage.TEACHERS_ROOT) = saved
        shutil.rmtree(tmpdir, ignore_errors=True)


async def _teacher(tid: int):
    await db_module.ensure_user(tid)
    await db_module.set_role(tid, ROLE_TEACHER)
    await db_module.set_subject(tid, SUBJECT)
    return await db_module.ensure_teacher(tid, SUBJECT)


async def _student(sid: int, teacher_id: int, name: str):
    await db_module.ensure_user(sid)
    await db_module.bind_student(sid, teacher_id, SUBJECT)
    await db_module.set_profile(sid, name=name)


class TestDates:
    def test_today_and_yesterday(self):
        now = _utcnow()
        assert _humanize_date(now) == "сегодня"
        assert _humanize_date(now - dt.timedelta(days=1)) == "вчера"

    def test_recent_days(self):
        assert _humanize_date(_utcnow() - dt.timedelta(days=4)) == "4 дн. назад"

    def test_old_date_shows_calendar(self):
        old = _utcnow() - dt.timedelta(days=40)
        assert _humanize_date(old) == old.strftime("%d.%m.%Y")

    def test_missing_date(self):
        assert _humanize_date(None) == "—"


class TestActivity:
    def test_never_active_is_not_active(self):
        assert not _is_active(User(telegram_id=1))

    def test_recent_is_active(self):
        assert _is_active(User(telegram_id=1, last_active_at=_utcnow() - dt.timedelta(days=2)))

    def test_old_is_not_active(self):
        assert not _is_active(User(telegram_id=1, last_active_at=_utcnow() - dt.timedelta(days=10)))



def _student_buttons(keyboard) -> list:
    """Только кнопки учеников: в клавиатуре есть ещё листалка и журнал."""
    return [
        b.text
        for row in keyboard.inline_keyboard
        for b in row
        if (b.callback_data or "").startswith("student:")
    ]


class TestStudentsList:
    async def test_button_per_student(self, env):
        await _teacher(TEACHER_A)
        await _student(STUDENT_1, TEACHER_A, "Ваня")
        await _student(STUDENT_2, TEACHER_A, "Оля")

        students = await db_module.get_teacher_students(TEACHER_A)
        assert _student_buttons(students_kb(students)) == ["Ваня", "Оля"]

    async def test_student_without_name_still_has_button(self, env):
        await _teacher(TEACHER_A)
        await db_module.ensure_user(STUDENT_1)
        await db_module.bind_student(STUDENT_1, TEACHER_A, SUBJECT)

        students = await db_module.get_teacher_students(TEACHER_A)
        assert _student_buttons(students_kb(students)) == [f"Ученик {STUDENT_1}"]

    async def test_empty_list_gives_empty_keyboard(self, env):
        await _teacher(TEACHER_A)
        students = await db_module.get_teacher_students(TEACHER_A)
        assert students_kb(students).inline_keyboard == []

    async def test_other_teachers_students_are_not_listed(self, env):
        await _teacher(TEACHER_A)
        await _teacher(TEACHER_B)
        await _student(STUDENT_1, TEACHER_A, "Ваня")
        await _student(STUDENT_OTHER, TEACHER_B, "Чужой")

        mine = await db_module.get_teacher_students(TEACHER_A)
        assert [s.name for s in mine] == ["Ваня"]


class TestPagination:
    """Большой класс не должен превращаться в простыню кнопок."""

    def _many(self, n: int):
        return [User(telegram_id=1000 + i, name=f"Ученик {i + 1}") for i in range(n)]

    def _texts(self, keyboard):
        return [b.text for row in keyboard.inline_keyboard for b in row]

    def test_no_navigation_when_everyone_fits(self):
        kb = students_kb(self._many(STUDENTS_PAGE_SIZE))
        assert "▶" not in self._texts(kb)
        # Строки учеников плюс одна на выгрузку журнала
        assert len(kb.inline_keyboard) == STUDENTS_PAGE_SIZE + 1

    def test_first_page_has_only_forward(self):
        texts = self._texts(students_kb(self._many(19), page=0))
        assert "▶" in texts and "◀" not in texts
        assert "1/3" in texts

    def test_middle_page_has_both_arrows(self):
        texts = self._texts(students_kb(self._many(19), page=1))
        assert "◀" in texts and "▶" in texts and "2/3" in texts

    def test_last_page_has_only_back(self):
        texts = self._texts(students_kb(self._many(19), page=2))
        assert "◀" in texts and "▶" not in texts

    def test_pages_do_not_overlap_and_cover_everyone(self):
        students = self._many(19)
        seen: list[str] = []
        for page in range(3):
            seen += _student_buttons(students_kb(students, page))
        assert seen == [s.name for s in students]

    def test_out_of_range_page_is_clamped(self):
        """Страница могла остаться в кнопке после ухода учеников."""
        texts = self._texts(students_kb(self._many(19), page=99))
        assert "3/3" in texts

    def test_card_returns_to_same_page(self):
        button = student_card_kb(page=2).inline_keyboard[0][0]
        assert button.callback_data == "students:list:2"

    def test_student_button_carries_page(self):
        rows = students_kb(self._many(19), page=1).inline_keyboard
        assert rows[0][0].callback_data.endswith(":1")


class TestStudentCardData:
    async def test_mistakes_are_counted_per_student(self, env):
        await _teacher(TEACHER_A)
        await _student(STUDENT_1, TEACHER_A, "Ваня")
        await _student(STUDENT_2, TEACHER_A, "Оля")

        for i in range(3):
            await db_module.add_mistake(
                STUDENT_1, {"question_text": f"вопрос {i}", "options": {}}, ""
            )

        assert await db_module.count_mistakes(STUDENT_1) == 3
        assert await db_module.count_mistakes(STUDENT_2) == 0

    async def test_completed_topics_counted(self, env):
        await _teacher(TEACHER_A)
        await _student(STUDENT_1, TEACHER_A, "Ваня")
        await db_module.mark_topic_completed(STUDENT_1, "1", "1_1")
        await db_module.mark_topic_completed(STUDENT_1, "1", "1_2")
        assert await db_module.count_completed_topics(STUDENT_1) == 2

    async def test_foreign_student_is_recognizable(self, env):
        """Карточка чужого ученика открываться не должна — проверка по teacher_id."""
        await _teacher(TEACHER_A)
        await _teacher(TEACHER_B)
        await _student(STUDENT_OTHER, TEACHER_B, "Чужой")

        student = await db_module.get_user(STUDENT_OTHER)
        assert student.teacher_id != TEACHER_A
