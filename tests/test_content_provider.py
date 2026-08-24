"""Тесты изоляции контента между преподавателями.

Главное, что здесь проверяется: ученик видит вопросы только своего
преподавателя. Если этот инвариант сломается, продукт теряет смысл.
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
from database.models import ROLE_STUDENT, ROLE_TEACHER  # noqa: E402
import subjects as subjects_cfg  # noqa: E402
from services import content_provider, storage  # noqa: E402

TEACHER_A, TEACHER_B = 9101, 9102
STUDENT_A, STUDENT_B, STUDENT_FREE = 9201, 9202, 9203
SUBJECT = "history"


def _rows(prefix: str, n: int):
    return [
        {
            "Вариант": f"{prefix}-вариант", "Часть": "А", "№": str(i),
            "Вопрос": f"{prefix}: вопрос номер {i} про историю",
            "Вар.1": "Первый", "Вар.2": "Второй", "Вар.3": "", "Вар.4": "", "Вар.5": "",
            "Ответ": "1", "Раздел": "",
        }
        for i in range(1, n + 1)
    ]


@pytest_asyncio.fixture
async def env():
    """Временная БД и временные папки контента."""
    tmpdir = Path(tempfile.mkdtemp(prefix="aida_cp_"))
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


async def _make_teacher(tid: int, rows_prefix: str | None, count: int = 3):
    await db_module.ensure_user(tid)
    await db_module.set_role(tid, ROLE_TEACHER)
    await db_module.set_subject(tid, SUBJECT)
    await db_module.ensure_teacher(tid, SUBJECT)
    storage.ensure_teacher_dirs(tid, SUBJECT)
    if rows_prefix is not None:
        storage.write_json(
            storage.teacher_tests_path(tid, SUBJECT),
            {"parser_version": 1, "rows": _rows(rows_prefix, count)},
        )


async def _make_student(sid: int, teacher_id: int | None):
    await db_module.ensure_user(sid)
    if teacher_id:
        await db_module.bind_student(sid, teacher_id, SUBJECT)
    else:
        await db_module.set_role(sid, ROLE_STUDENT)
        await db_module.set_subject(sid, SUBJECT)


class TestIsolation:
    async def test_student_sees_only_own_teacher_content(self, env):
        await _make_teacher(TEACHER_A, "А", 3)
        await _make_teacher(TEACHER_B, "Б", 5)
        await _make_student(STUDENT_A, TEACHER_A)
        await _make_student(STUDENT_B, TEACHER_B)

        bundle_a = await content_provider.get_tests(STUDENT_A)
        bundle_b = await content_provider.get_tests(STUDENT_B)

        assert len(bundle_a.rows) == 3
        assert len(bundle_b.rows) == 5
        assert bundle_a.teacher_id == TEACHER_A
        assert bundle_b.teacher_id == TEACHER_B

        texts_a = {r["Вопрос"] for r in bundle_a.rows}
        texts_b = {r["Вопрос"] for r in bundle_b.rows}
        assert not (texts_a & texts_b)

    async def test_teacher_sees_own_content(self, env):
        """Преподаватель проходит собственный тренажёр без отдельной ветки кода."""
        await _make_teacher(TEACHER_A, "А", 4)
        bundle = await content_provider.get_tests(TEACHER_A)
        assert len(bundle.rows) == 4
        assert bundle.teacher_id == TEACHER_A

    async def test_unbound_student_gets_no_content(self, env):
        await _make_student(STUDENT_FREE, None)
        bundle = await content_provider.get_tests(STUDENT_FREE)
        assert bundle.rows == []
        assert bundle.empty_reason == content_provider.NO_TEACHER
        assert not bundle

    async def test_student_of_empty_teacher(self, env):
        """Если и общей базы нет — честно объясняем, что материалов пока нет."""
        await _make_teacher(TEACHER_A, None)
        await _make_student(STUDENT_A, TEACHER_A)
        bundle = await content_provider.get_tests(STUDENT_A)
        assert bundle.rows == []
        assert bundle.empty_reason == content_provider.TEACHER_HAS_NO_CONTENT

    async def test_teacher_without_uploads_gets_own_hint(self, env):
        """Преподавателю и ученику подсказки разные — им нужны разные действия."""
        await _make_teacher(TEACHER_A, None)
        bundle = await content_provider.get_tests(TEACHER_A)
        assert bundle.empty_reason == content_provider.OWN_CONTENT_EMPTY

    async def test_unknown_user(self, env):
        bundle = await content_provider.get_tests(999999)
        assert bundle.rows == []
        assert bundle.empty_reason


class TestBaseFallback:
    """Пока преподаватель не загрузил своё, тренажёр работает на общей базе."""

    async def test_teacher_without_uploads_gets_base_questions(self, env, monkeypatch):
        monkeypatch.setattr(
            content_provider.sheets_cache, "tests_rows", _rows("общий", 5), raising=False
        )
        await _make_teacher(TEACHER_A, None)

        bundle = await content_provider.get_tests(TEACHER_A)
        assert len(bundle.rows) == 5
        assert bundle.fallback is True
        assert bundle.source == subjects_cfg.SOURCE_BASE

    async def test_student_of_empty_teacher_gets_base_questions(self, env, monkeypatch):
        monkeypatch.setattr(
            content_provider.sheets_cache, "tests_rows", _rows("общий", 4), raising=False
        )
        await _make_teacher(TEACHER_A, None)
        await _make_student(STUDENT_A, TEACHER_A)

        bundle = await content_provider.get_tests(STUDENT_A)
        assert len(bundle.rows) == 4
        assert bundle.fallback is True

    async def test_unbound_student_never_gets_base(self, env, monkeypatch):
        """Без преподавателя общий набор не подставляется.

        Иначе ученик, обошедший ввод кода, получает рабочий тренажёр
        и уже не привяжется ни к кому.
        """
        monkeypatch.setattr(
            content_provider.sheets_cache, "tests_rows", _rows("общий", 20), raising=False
        )
        await _make_student(STUDENT_FREE, None)

        bundle = await content_provider.get_tests(STUDENT_FREE)
        assert bundle.rows == []
        assert bundle.empty_reason == content_provider.NO_TEACHER

    async def test_own_content_wins_over_base(self, env, monkeypatch):
        """Как только преподаватель загрузил своё — общее больше не подмешивается."""
        monkeypatch.setattr(
            content_provider.sheets_cache, "tests_rows", _rows("общий", 50), raising=False
        )
        await _make_teacher(TEACHER_A, "А", 3)
        await _make_student(STUDENT_A, TEACHER_A)

        bundle = await content_provider.get_tests(STUDENT_A)
        assert len(bundle.rows) == 3
        assert bundle.fallback is False
        assert bundle.source == subjects_cfg.SOURCE_TEACHER

    async def test_isolation_holds_with_fallback_enabled(self, env, monkeypatch):
        """Общая база — не лазейка: чужие вопросы всё равно не видны."""
        monkeypatch.setattr(
            content_provider.sheets_cache, "tests_rows", _rows("общий", 10), raising=False
        )
        await _make_teacher(TEACHER_A, "А", 3)
        await _make_teacher(TEACHER_B, "Б", 4)
        await _make_student(STUDENT_A, TEACHER_A)
        await _make_student(STUDENT_B, TEACHER_B)

        rows_a = (await content_provider.get_tests(STUDENT_A)).rows
        rows_b = (await content_provider.get_tests(STUDENT_B)).rows
        assert not ({r["Вопрос"] for r in rows_a} & {r["Вопрос"] for r in rows_b})


class TestTeacherSourceSetting:
    """Преподаватель сам выбирает, на чём занимаются его ученики."""

    async def _set(self, teacher_id: int, value: str):
        await db_module.set_teacher_setting(
            teacher_id, SUBJECT, content_provider.MATERIALS_SOURCE, value
        )

    async def test_default_is_own_then_base(self, env):
        await _make_teacher(TEACHER_A, None)
        user = await db_module.get_user(TEACHER_A)
        source = await content_provider.effective_source(user, SUBJECT)
        assert source == subjects_cfg.SOURCE_TEACHER_THEN_BASE

    async def test_only_own_disables_base_fallback(self, env, monkeypatch):
        """Строгий режим: нет своих вопросов — значит нет вопросов вовсе."""
        monkeypatch.setattr(
            content_provider.sheets_cache, "tests_rows", _rows("общий", 9), raising=False
        )
        await _make_teacher(TEACHER_A, None)
        await self._set(TEACHER_A, subjects_cfg.SOURCE_TEACHER)

        bundle = await content_provider.get_tests(TEACHER_A)
        assert bundle.rows == []
        assert bundle.empty_reason == content_provider.OWN_CONTENT_EMPTY

    async def test_only_base_ignores_own_content(self, env, monkeypatch):
        monkeypatch.setattr(
            content_provider.sheets_cache, "tests_rows", _rows("общий", 6), raising=False
        )
        await _make_teacher(TEACHER_A, "А", 3)
        await self._set(TEACHER_A, subjects_cfg.SOURCE_BASE)

        bundle = await content_provider.get_tests(TEACHER_A)
        assert len(bundle.rows) == 6
        assert bundle.source == subjects_cfg.SOURCE_BASE

    async def test_student_inherits_teacher_choice(self, env, monkeypatch):
        """Ученик не настраивает ничего — источник задаёт его преподаватель."""
        monkeypatch.setattr(
            content_provider.sheets_cache, "tests_rows", _rows("общий", 6), raising=False
        )
        await _make_teacher(TEACHER_A, "А", 3)
        await _make_student(STUDENT_A, TEACHER_A)
        await self._set(TEACHER_A, subjects_cfg.SOURCE_BASE)

        bundle = await content_provider.get_tests(STUDENT_A)
        assert len(bundle.rows) == 6
        assert bundle.source == subjects_cfg.SOURCE_BASE

    async def test_unavailable_value_falls_back_to_default(self, env):
        """Недоступный источник игнорируется — например, после смены тарифа."""
        await _make_teacher(TEACHER_A, "А", 2)
        await self._set(TEACHER_A, "несуществующий")

        user = await db_module.get_user(TEACHER_A)
        source = await content_provider.effective_source(user, SUBJECT)
        assert source == subjects_cfg.SOURCE_TEACHER_THEN_BASE

    async def test_setting_is_per_teacher(self, env, monkeypatch):
        monkeypatch.setattr(
            content_provider.sheets_cache, "tests_rows", _rows("общий", 8), raising=False
        )
        await _make_teacher(TEACHER_A, "А", 3)
        await _make_teacher(TEACHER_B, "Б", 5)
        await self._set(TEACHER_A, subjects_cfg.SOURCE_BASE)

        assert len((await content_provider.get_tests(TEACHER_A)).rows) == 8
        assert len((await content_provider.get_tests(TEACHER_B)).rows) == 5


class TestAllowedSources:
    def test_subject_without_base_offers_only_own(self):
        assert subjects_cfg.allowed_sources("russian") == [subjects_cfg.SOURCE_TEACHER]

    def test_subject_with_base_offers_all_three(self):
        assert len(subjects_cfg.allowed_sources("history")) == 3

    async def test_default_is_clamped_to_allowed(self, env):
        """У предмета без общей базы умолчание «base» недостижимо.

        Без приведения к доступному преподаватель русского получил бы
        вопросы по истории.
        """
        await db_module.ensure_user(TEACHER_A)
        await db_module.set_role(TEACHER_A, ROLE_TEACHER)
        await db_module.set_subject(TEACHER_A, "russian")
        await db_module.ensure_teacher(TEACHER_A, "russian")

        user = await db_module.get_user(TEACHER_A)
        source = await content_provider.effective_source(user, "russian")
        assert source in subjects_cfg.allowed_sources("russian")


class TestMistakesIsolation:
    async def test_mistakes_cleared_when_student_changes_teacher(self, env):
        """Ошибки хранят вопросы прежнего преподавателя целиком.

        Если их не убрать, режим «Мои ошибки» покажет чужие материалы —
        это ломает главный инвариант продукта.
        """
        await _make_teacher(TEACHER_A, "А", 3)
        await _make_teacher(TEACHER_B, "Б", 3)
        await _make_student(STUDENT_A, TEACHER_A)

        await db_module.add_mistake(
            STUDENT_A, {"question_text": "вопрос преподавателя А", "options": {}}, ""
        )
        assert await db_module.count_mistakes(STUDENT_A) == 1

        await db_module.bind_student(STUDENT_A, TEACHER_B, SUBJECT)
        assert await db_module.count_mistakes(STUDENT_A) == 0

    async def test_mistakes_survive_rebinding_to_same_teacher(self, env):
        """Повторный переход по той же ссылке не должен стирать прогресс."""
        await _make_teacher(TEACHER_A, "А", 3)
        await _make_student(STUDENT_A, TEACHER_A)
        await db_module.add_mistake(
            STUDENT_A, {"question_text": "какой-то вопрос", "options": {}}, ""
        )

        await db_module.bind_student(STUDENT_A, TEACHER_A, SUBJECT)
        assert await db_module.count_mistakes(STUDENT_A) == 1

    async def test_first_binding_keeps_nothing_to_lose(self, env):
        await _make_teacher(TEACHER_A, "А", 3)
        await db_module.ensure_user(STUDENT_A)
        await db_module.bind_student(STUDENT_A, TEACHER_A, SUBJECT)
        student = await db_module.get_user(STUDENT_A)
        assert student.teacher_id == TEACHER_A


class TestFlashcardsFallback:
    def test_empty_teacher_file_falls_back_to_base(self, env, monkeypatch):
        """Пустой файл карточек — это не «свои карточки»."""
        monkeypatch.setitem(
            subjects_cfg.SUBJECTS["history"]["sources"],
            "flashcards",
            subjects_cfg.SOURCE_TEACHER,
        )
        monkeypatch.setattr(
            content_provider.sheets_cache, "flash_dates",
            [{"Дата": "1569", "Событие": "Люблинская уния"}], raising=False,
        )
        storage.ensure_teacher_dirs(TEACHER_A, SUBJECT)
        storage.write_json(
            storage.teacher_flashcards_path(TEACHER_A, SUBJECT),
            {"dates": [], "concepts": [], "persons": []},
        )

        cards = content_provider.get_flashcards(SUBJECT, teacher_id=TEACHER_A)
        assert cards["dates"][0]["Дата"] == "1569"


class TestVariants:
    def test_variants_are_unique_and_sorted(self):
        rows = [
            {"Вариант": "Б"}, {"Вариант": "А"}, {"Вариант": "Б"},
            {"Вариант": ""}, {},
        ]
        assert content_provider.variants_of(rows) == ["А", "Б"]


class TestSourcePolicy:
    async def test_history_tests_come_from_teacher(self, env):
        import subjects as subjects_cfg

        await _make_teacher(TEACHER_A, "А", 2)
        await _make_student(STUDENT_A, TEACHER_A)
        bundle = await content_provider.get_tests(STUDENT_A)
        assert bundle.source == subjects_cfg.SOURCE_TEACHER

    async def test_base_policy_falls_back_to_shared_content(self, env, monkeypatch):
        """При политике base вопросы берутся из общего слоя предмета."""
        import subjects as subjects_cfg

        monkeypatch.setitem(
            subjects_cfg.SUBJECTS["history"]["sources"], "tests", subjects_cfg.SOURCE_BASE
        )
        fake_rows = _rows("общий", 7)
        monkeypatch.setattr(content_provider.sheets_cache, "tests_rows", fake_rows, raising=False)

        await _make_student(STUDENT_FREE, None)
        bundle = await content_provider.get_tests(STUDENT_FREE)
        assert bundle.source == subjects_cfg.SOURCE_BASE
        assert len(bundle.rows) == 7
