"""Сквозной прогон демо на настоящих билетах.

Это тот самый сценарий, который показываем: преподаватель загружает файл,
раздаёт ссылку, ученик присоединяется и видит его вопросы. Если этот тест
падает — демо не состоится.
"""

import shutil
import sys
import tempfile
from pathlib import Path

import pytest
import pytest_asyncio

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import database.db as db_module  # noqa: E402
from database.models import ROLE_TEACHER  # noqa: E402
from services import content_provider, storage, teacher_content  # noqa: E402

SAMPLES = ROOT / "data/raw/tests"
SUBJECT = "history"

TEACHER_A, TEACHER_B = 9601, 9602
STUDENT_A, STUDENT_B = 9701, 9702

pytestmark = pytest.mark.skipif(
    not (SAMPLES / "Раннее Новое время.docx").exists(),
    reason="нет образцов билетов",
)


@pytest_asyncio.fixture
async def env():
    tmpdir = Path(tempfile.mkdtemp(prefix="aida_demo_"))
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


async def _register_teacher(tid: int, files: list[str]):
    await db_module.ensure_user(tid)
    await db_module.set_role(tid, ROLE_TEACHER)
    await db_module.set_subject(tid, SUBJECT)
    teacher = await db_module.ensure_teacher(tid, SUBJECT)
    storage.ensure_teacher_dirs(tid, SUBJECT)
    for name in files:
        teacher_content.store_upload(tid, SUBJECT, SAMPLES / name, name)
    result = teacher_content.rebuild(tid, SUBJECT)
    await db_module.set_teacher_content(
        tid, SUBJECT, "tests",
        path=str(storage.teacher_tests_path(tid, SUBJECT)),
        items_count=result.total_accepted,
    )
    return teacher, result


async def _join_by_code(student_id: int, code: str):
    await db_module.ensure_user(student_id)
    teacher = await db_module.get_teacher_by_invite(code)
    assert teacher is not None, "код приглашения не сработал"
    await db_module.bind_student(student_id, teacher.tg_user_id, teacher.subject)
    return teacher


class TestFullDemo:
    async def test_teacher_uploads_and_student_trains(self, env):
        teacher, result = await _register_teacher(TEACHER_A, ["Раннее Новое время.docx"])

        # Файл разобран, вопросы приняты
        assert result.total_accepted == 34
        assert result.total_rejected == 0

        # Картинки вычищены — оригинал остался пригодным для перепарсивания
        uploads = list(storage.teacher_uploads_dir(TEACHER_A, SUBJECT).glob("*.docx"))
        assert len(uploads) == 1

        # Ученик присоединяется по коду и видит вопросы преподавателя
        await _join_by_code(STUDENT_A, teacher.invite_code)
        bundle = await content_provider.get_tests(STUDENT_A)
        assert len(bundle.rows) == 34
        assert bundle.teacher_id == TEACHER_A

        # Каждый вопрос пригоден для прохождения: есть текст и ответ
        for row in bundle.rows:
            assert row["Вопрос"].strip()
            assert row["Ответ"].strip()

    async def test_two_teachers_do_not_mix(self, env):
        a, res_a = await _register_teacher(TEACHER_A, ["Раннее Новое время.docx"])
        b, res_b = await _register_teacher(
            TEACHER_B, ["2 XIX век-1945 гг.docx", "3. 1918-1945.docx"]
        )
        assert res_a.total_accepted == 34
        assert res_b.total_accepted == 67

        await _join_by_code(STUDENT_A, a.invite_code)
        await _join_by_code(STUDENT_B, b.invite_code)

        rows_a = (await content_provider.get_tests(STUDENT_A)).rows
        rows_b = (await content_provider.get_tests(STUDENT_B)).rows

        assert len(rows_a) == 34 and len(rows_b) == 67
        assert not ({r["Вопрос"] for r in rows_a} & {r["Вопрос"] for r in rows_b})

    async def test_reparse_rebuilds_without_reupload(self, env):
        """Правки парсера применяются к накопленному без участия преподавателя."""
        await _register_teacher(TEACHER_A, ["Раннее Новое время.docx"])
        before = len(teacher_content.load_tests(TEACHER_A, SUBJECT))

        # Имитируем то, что делает /reparse
        again = teacher_content.rebuild(TEACHER_A, SUBJECT)
        assert again.total_accepted == before

    async def test_file_without_answer_key_is_reported(self, env):
        """Файл без ключа не должен молча превращаться в пустой тренажёр."""
        _teacher, result = await _register_teacher(TEACHER_A, ["variant_1.docx"])

        assert result.total_accepted == 0
        assert result.total_found == 38
        no_key = result.files_without_key()
        assert [f.filename for f in no_key] == ["variant_1.docx"]

    async def test_teacher_can_preview_own_trainer(self, env):
        await _register_teacher(TEACHER_A, ["Раннее Новое время.docx"])
        bundle = await content_provider.get_tests(TEACHER_A)
        assert len(bundle.rows) == 34
