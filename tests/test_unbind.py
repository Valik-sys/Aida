"""Отвязка ученика от преподавателя.

Просили на фокус-группе: ко второму сезону список учеников зарастает
теми, кто уже не занимается. С тарифами по числу учеников это ещё и
деньги — за ушедших платить не за что.

Главное, что здесь проверяется: **отвязать можно только своего ученика.**
Кнопки живут в чате вечно, и по старой кнопке с чужим номером нельзя
отцепить ученика у другого преподавателя.
"""

import shutil
import sys
import tempfile
from pathlib import Path

import pytest_asyncio

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import database.db as db_module  # noqa: E402
from bot.keyboards.inline import student_card_kb, unbind_confirm_kb  # noqa: E402
from database.models import ROLE_TEACHER  # noqa: E402
from services import content_provider, storage  # noqa: E402

TEACHER_A, TEACHER_B = 7701, 7702
STUDENT_1, STUDENT_2 = 7801, 7802
SUBJECT = "history"


@pytest_asyncio.fixture
async def env():
    tmpdir = Path(tempfile.mkdtemp(prefix="aida_unbind_"))
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


async def _student(sid: int, teacher_id: int, name: str = "Ученик"):
    await db_module.ensure_user(sid)
    await db_module.bind_student(sid, teacher_id, SUBJECT)
    await db_module.set_profile(sid, name=name)


class TestUnbind:
    async def test_own_student_is_unbound(self, env):
        await _teacher(TEACHER_A)
        await _student(STUDENT_1, TEACHER_A)

        assert await db_module.unbind_student(STUDENT_1, TEACHER_A) is True

        user = await db_module.get_user(STUDENT_1)
        assert user.teacher_id is None

    async def test_student_leaves_the_list(self, env):
        await _teacher(TEACHER_A)
        await _student(STUDENT_1, TEACHER_A, "Ваня")
        await _student(STUDENT_2, TEACHER_A, "Оля")

        await db_module.unbind_student(STUDENT_1, TEACHER_A)

        left = await db_module.get_teacher_students(TEACHER_A)
        assert [s.name for s in left] == ["Оля"]
        assert await db_module.count_teacher_students(TEACHER_A) == 1

    async def test_someone_elses_student_is_untouched(self, env):
        """Главный инвариант: чужого отцепить нельзя."""
        await _teacher(TEACHER_A)
        await _teacher(TEACHER_B)
        await _student(STUDENT_1, TEACHER_B, "Чужой")

        assert await db_module.unbind_student(STUDENT_1, TEACHER_A) is False

        user = await db_module.get_user(STUDENT_1)
        assert user.teacher_id == TEACHER_B

    async def test_second_press_changes_nothing(self, env):
        """По кнопке из старого сообщения нажмут ещё раз."""
        await _teacher(TEACHER_A)
        await _student(STUDENT_1, TEACHER_A)

        assert await db_module.unbind_student(STUDENT_1, TEACHER_A) is True
        assert await db_module.unbind_student(STUDENT_1, TEACHER_A) is False

    async def test_unknown_student_is_refused(self, env):
        await _teacher(TEACHER_A)
        assert await db_module.unbind_student(999999, TEACHER_A) is False


class TestAfterUnbind:
    async def test_materials_are_gone_at_once(self, env):
        """Ради этого всё и делается: доступ к материалам пропадает сразу."""
        await _teacher(TEACHER_A)
        await _student(STUDENT_1, TEACHER_A)

        assert content_provider.resolve_teacher_id(
            await db_module.get_user(STUDENT_1)
        ) == TEACHER_A

        await db_module.unbind_student(STUDENT_1, TEACHER_A)

        assert content_provider.resolve_teacher_id(
            await db_module.get_user(STUDENT_1)
        ) is None

    async def test_progress_survives(self, env):
        """Прогресс — работа ученика, а не имущество преподавателя.

        Он мог уйти на лето и вернуться по новому приглашению.
        """
        await _teacher(TEACHER_A)
        await _student(STUDENT_1, TEACHER_A)
        await db_module.record_answer(
            STUDENT_1, "Кто основал город?", True,
            teacher_id=TEACHER_A, subject=SUBJECT,
        )

        await db_module.unbind_student(STUDENT_1, TEACHER_A)

        progress = await db_module.get_student_progress(STUDENT_1)
        assert progress["attempts"] == 1

    async def test_can_be_bound_again(self, env):
        await _teacher(TEACHER_A)
        await _student(STUDENT_1, TEACHER_A)
        await db_module.unbind_student(STUDENT_1, TEACHER_A)

        await db_module.bind_student(STUDENT_1, TEACHER_A, SUBJECT)

        user = await db_module.get_user(STUDENT_1)
        assert user.teacher_id == TEACHER_A


class TestScreens:
    def test_card_offers_unbinding(self):
        buttons = [b for row in student_card_kb(0, STUDENT_1).inline_keyboard for b in row]
        assert any(b.callback_data == f"unbind:ask:{STUDENT_1}:0" for b in buttons)

    def test_card_without_student_has_no_unbind(self):
        """Карточка зовётся и без номера — тогда кнопки быть не должно."""
        buttons = [b for row in student_card_kb(0).inline_keyboard for b in row]
        assert not any((b.callback_data or "").startswith("unbind:") for b in buttons)

    def test_confirmation_asks_before_doing(self):
        rows = unbind_confirm_kb(STUDENT_1, page=2).inline_keyboard
        first = rows[0][0]

        # Отмена первой: случайное нажатие не должно отрезать ученика
        assert first.text == "Отмена"
        assert first.callback_data == f"student:{STUDENT_1}:2"
        assert rows[1][0].callback_data == f"unbind:do:{STUDENT_1}:2"

    def test_confirmation_keeps_the_page(self):
        """После отвязки возвращаемся на ту же страницу списка."""
        rows = unbind_confirm_kb(STUDENT_1, page=3).inline_keyboard
        assert rows[1][0].callback_data.endswith(":3")
