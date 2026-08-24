"""Приглашения и привязка ученика к преподавателю."""

import shutil
import sys
import tempfile
from pathlib import Path

import pytest_asyncio

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import database.db as db_module  # noqa: E402
from database.models import ROLE_TEACHER  # noqa: E402
from services import content_provider, storage  # noqa: E402

T_A, T_B = 9401, 9402
S_ONE, S_TWO = 9501, 9502
SUBJECT = "history"


@pytest_asyncio.fixture
async def env():
    tmpdir = Path(tempfile.mkdtemp(prefix="aida_inv_"))
    saved = (
        db_module.DB_PATH,
        storage.DATA_ROOT, storage.SUBJECTS_ROOT, storage.TEACHERS_ROOT,
    )
    db_module.DB_PATH = str(tmpdir / "test.sqlite3")
    storage.DATA_ROOT = tmpdir / "data"
    storage.SUBJECTS_ROOT = storage.DATA_ROOT / "subjects"
    storage.TEACHERS_ROOT = storage.DATA_ROOT / "teachers"
    try:
        await db_module.init_db()
        yield tmpdir
    finally:
        (
            db_module.DB_PATH,
            storage.DATA_ROOT, storage.SUBJECTS_ROOT, storage.TEACHERS_ROOT,
        ) = saved
        shutil.rmtree(tmpdir, ignore_errors=True)


async def _teacher(tid: int, subject: str = SUBJECT):
    await db_module.ensure_user(tid)
    await db_module.set_role(tid, ROLE_TEACHER)
    await db_module.set_subject(tid, subject)
    storage.ensure_teacher_dirs(tid, subject)
    return await db_module.ensure_teacher(tid, subject)


class TestInviteCodes:
    async def test_code_is_stable_across_calls(self, env):
        """Код нельзя менять: разосланные ученикам ссылки перестали бы работать."""
        first = await _teacher(T_A)
        second = await db_module.ensure_teacher(T_A, SUBJECT)
        assert first.invite_code == second.invite_code

    async def test_codes_are_unique_between_teachers(self, env):
        a = await _teacher(T_A)
        b = await _teacher(T_B)
        assert a.invite_code != b.invite_code

    async def test_lookup_is_case_insensitive(self, env):
        """Код диктуют голосом и вводят руками — регистр значения не имеет."""
        teacher = await _teacher(T_A)
        found = await db_module.get_teacher_by_invite(teacher.invite_code.lower())
        assert found and found.tg_user_id == T_A

    async def test_unknown_code_returns_nothing(self, env):
        await _teacher(T_A)
        assert await db_module.get_teacher_by_invite("ZZZZZZ") is None
        assert await db_module.get_teacher_by_invite("") is None

    async def test_code_has_no_confusable_characters(self, env):
        """Ни нулей с буквой O, ни единиц с I — код переписывают руками."""
        teacher = await _teacher(T_A)
        assert not (set(teacher.invite_code) & set("O0I1L"))

    async def test_code_carries_subject(self, env):
        """Предмет ученику достаётся от преподавателя, а не выбирается им."""
        teacher = await _teacher(T_A, "history")
        found = await db_module.get_teacher_by_invite(teacher.invite_code)
        assert found.subject == "history"


class TestBinding:
    async def test_student_binds_and_gets_subject(self, env):
        teacher = await _teacher(T_A)
        await db_module.ensure_user(S_ONE)
        await db_module.bind_student(S_ONE, teacher.tg_user_id, teacher.subject)

        student = await db_module.get_user(S_ONE)
        assert student.is_student
        assert student.teacher_id == T_A
        assert student.current_subject == SUBJECT

    async def test_rebinding_moves_student_to_new_teacher(self, env):
        """Ученик перешёл к другому преподавателю — старый его больше не видит."""
        a = await _teacher(T_A)
        b = await _teacher(T_B)
        await db_module.ensure_user(S_ONE)

        await db_module.bind_student(S_ONE, a.tg_user_id, a.subject)
        assert await db_module.count_teacher_students(T_A) == 1

        await db_module.bind_student(S_ONE, b.tg_user_id, b.subject)
        assert await db_module.count_teacher_students(T_A) == 0
        assert await db_module.count_teacher_students(T_B) == 1

    async def test_students_are_counted_per_teacher(self, env):
        a = await _teacher(T_A)
        await _teacher(T_B)
        for sid in (S_ONE, S_TWO):
            await db_module.ensure_user(sid)
            await db_module.bind_student(sid, a.tg_user_id, a.subject)

        assert await db_module.count_teacher_students(T_A) == 2
        assert await db_module.count_teacher_students(T_B) == 0

    async def test_teacher_list_shows_names(self, env):
        a = await _teacher(T_A)
        await db_module.ensure_user(S_ONE)
        await db_module.bind_student(S_ONE, a.tg_user_id, a.subject)
        await db_module.set_profile(S_ONE, name="Ваня", class_name="11")

        students = await db_module.get_teacher_students(T_A)
        assert [s.name for s in students] == ["Ваня"]
        assert students[0].class_name == "11"


class TestIsolationAfterBinding:
    async def test_moved_student_sees_new_teacher_content(self, env):
        """После перехода ученик должен видеть контент нового преподавателя."""
        a = await _teacher(T_A)
        b = await _teacher(T_B)

        for tid, prefix in ((T_A, "А"), (T_B, "Б")):
            storage.write_json(
                storage.teacher_tests_path(tid, SUBJECT),
                {"parser_version": 1, "rows": [{
                    "Вариант": prefix, "Часть": "А", "№": "1",
                    "Вопрос": f"Вопрос преподавателя {prefix}",
                    "Вар.1": "Раз", "Вар.2": "Два", "Вар.3": "", "Вар.4": "", "Вар.5": "",
                    "Ответ": "1", "Раздел": "",
                }]},
            )

        await db_module.ensure_user(S_ONE)
        await db_module.bind_student(S_ONE, a.tg_user_id, a.subject)
        bundle = await content_provider.get_tests(S_ONE)
        assert bundle.rows[0]["Вопрос"].endswith("А")

        await db_module.bind_student(S_ONE, b.tg_user_id, b.subject)
        bundle = await content_provider.get_tests(S_ONE)
        assert bundle.rows[0]["Вопрос"].endswith("Б")
