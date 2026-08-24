"""Три давних дефекта, найденные при ревью.

Все три тихие: ничего не падает, но данные врут — вопросы исчезают,
статистика считается по чужим ответам, экран показывает не тот режим.
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
import subjects as subjects_cfg  # noqa: E402
from database.models import ROLE_TEACHER  # noqa: E402
from services import content_provider, docx_tools, storage  # noqa: E402

SUBJECT = "history"
TEACHER_A, TEACHER_B = 6601, 6602
STUDENT = 6701


class TestSafeStem:
    """Длинные имена файлов не должны схлопываться в один путь."""

    def test_short_names_unchanged(self):
        assert docx_tools.safe_stem("Раннее Новое время.docx") == "Раннее Новое время"

    def test_bad_characters_replaced(self):
        assert docx_tools.safe_stem('би*ле:ты?.docx') == "би_ле_ты_"

    def test_path_prefix_dropped(self):
        # Path отбрасывает всё до разделителя как каталог — на вход может
        # прийти имя с путём, в uploads должно лечь только само имя
        assert docx_tools.safe_stem("папка/билеты.docx") == "билеты"

    def test_empty_name_has_a_fallback(self):
        assert docx_tools.safe_stem("") == "file"
        assert docx_tools.safe_stem("....docx") == "file"

    def test_long_similar_names_do_not_collide(self):
        # Первые 80 символов совпадают — раньше второй файл ложился
        # на место первого, и его вопросы исчезали при пересборке
        base = "Обобщение по разделам с четвёртого по восьмой для подготовки к экзамену часть"
        first = docx_tools.safe_stem(f"{base} первая.docx")
        second = docx_tools.safe_stem(f"{base} вторая.docx")

        assert first != second
        assert len(first) <= docx_tools.MAX_STEM
        assert len(second) <= docx_tools.MAX_STEM

    def test_same_name_gives_the_same_path(self):
        # Иначе повторная загрузка того же файла двоила бы вопросы
        long_name = "Очень длинное название файла с билетами по истории Беларуси за весь курс подготовки.docx"
        assert docx_tools.safe_stem(long_name) == docx_tools.safe_stem(long_name)


@pytest_asyncio.fixture
async def env():
    tmpdir = Path(tempfile.mkdtemp(prefix="aida_review_"))
    saved = (db_module.DB_PATH, storage.DATA_ROOT, storage.TEACHERS_ROOT)
    db_module.DB_PATH = str(tmpdir / "test.sqlite3")
    storage.DATA_ROOT = tmpdir / "data"
    storage.TEACHERS_ROOT = storage.DATA_ROOT / "teachers"
    try:
        await db_module.init_db()
        for tid in (TEACHER_A, TEACHER_B):
            await db_module.ensure_user(tid)
            await db_module.set_role(tid, ROLE_TEACHER)
            await db_module.set_subject(tid, SUBJECT)
            await db_module.ensure_teacher(tid, SUBJECT)
        await db_module.ensure_user(STUDENT)
        await db_module.set_profile(STUDENT, name="Аня")
        yield tmpdir
    finally:
        (db_module.DB_PATH, storage.DATA_ROOT, storage.TEACHERS_ROOT) = saved
        shutil.rmtree(tmpdir, ignore_errors=True)


async def _answer(student: int, teacher: int, question: str, correct: bool):
    await db_module.record_answer(
        student, question, is_correct=correct,
        teacher_id=teacher, subject=SUBJECT, student_answer="1",
    )


class TestStudentsStats:
    """Сводка по ученикам считается по ответам этому преподавателю."""

    async def test_answers_to_a_previous_teacher_are_not_counted(self, env):
        await db_module.bind_student(STUDENT, TEACHER_A, SUBJECT)
        await _answer(STUDENT, TEACHER_A, "вопрос первого", correct=False)
        await _answer(STUDENT, TEACHER_A, "второй вопрос", correct=False)

        # Ученик ушёл к другому преподавателю и отвечает уже ему
        await db_module.bind_student(STUDENT, TEACHER_B, SUBJECT)
        await _answer(STUDENT, TEACHER_B, "вопрос второго", correct=True)

        stats = await db_module.get_students_stats(TEACHER_B, SUBJECT)
        mine = next(s for s in stats if s["telegram_id"] == STUDENT)

        # Раньше сюда попадали и два неверных ответа прежнему преподавателю,
        # а средний по классу считался только по своим — сравнивали разное
        assert mine["attempts"] == 1
        assert mine["accuracy"] == 100

    async def test_matches_the_class_average_source(self, env):
        await db_module.bind_student(STUDENT, TEACHER_A, SUBJECT)
        await _answer(STUDENT, TEACHER_A, "свой вопрос", correct=True)
        await _answer(STUDENT, TEACHER_B, "чужой вопрос", correct=False)

        stats = await db_module.get_students_stats(TEACHER_A, SUBJECT)
        totals = await db_module.get_teacher_totals(TEACHER_A, SUBJECT)
        mine = next(s for s in stats if s["telegram_id"] == STUDENT)

        assert mine["attempts"] == totals["attempts"]

    async def test_student_without_answers_stays_in_the_list(self, env):
        await db_module.bind_student(STUDENT, TEACHER_A, SUBJECT)

        stats = await db_module.get_students_stats(TEACHER_A, SUBJECT)
        mine = next(s for s in stats if s["telegram_id"] == STUDENT)

        assert mine["attempts"] == 0
        assert mine["accuracy"] is None


class TestSourceScreen:
    """Экран «Материалы тренажёра» показывает то, чем тренажёр и пользуется."""

    async def test_agrees_with_the_trainer(self, env):
        from bot.handlers.menu import _source_screen

        user = await db_module.get_user(TEACHER_A)
        real = await content_provider.effective_source(user, SUBJECT)
        text, _markup = await _source_screen(TEACHER_A, SUBJECT)

        assert subjects_cfg.SOURCE_LABELS[real] in text

    async def test_unavailable_setting_is_ignored(self, env):
        from bot.handlers.menu import _source_screen

        # В базе может остаться режим, которого у предмета уже нет:
        # тренажёр его не берёт, и экран не должен показывать
        await db_module.set_teacher_setting(
            TEACHER_A, "russian", content_provider.MATERIALS_SOURCE,
            subjects_cfg.SOURCE_BASE,
        )
        allowed = subjects_cfg.allowed_sources("russian")
        assert subjects_cfg.SOURCE_BASE not in allowed

        text, markup = await _source_screen(TEACHER_A, "russian")

        assert subjects_cfg.SOURCE_LABELS[allowed[0]] in text
        # И выбранный вариант отмечен галочкой, а не потерян
        marks = [b.text for row in markup.inline_keyboard for b in row]
        assert any(m.startswith("✓") for m in marks)
