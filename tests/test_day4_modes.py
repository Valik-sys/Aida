"""Режимы по предметам: карточки, «Спросить», превью тренажёра преподавателя."""

import shutil
import sys
import tempfile
from pathlib import Path

import pytest_asyncio

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import database.db as db_module  # noqa: E402
import subjects as subjects_cfg  # noqa: E402
from database.models import ROLE_TEACHER  # noqa: E402
from services import content_provider, storage, teacher_content  # noqa: E402

TEACHER = 9301
SUBJECT = "history"


@pytest_asyncio.fixture
async def env():
    tmpdir = Path(tempfile.mkdtemp(prefix="aida_d4_"))
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


class TestSubjectModes:
    def test_history_modes(self):
        modes = subjects_cfg.visible_modes("history")
        assert modes == ["tests", "flashcards", "ask", "mistakes", "progress"]

    def test_russian_has_only_ask(self):
        assert subjects_cfg.visible_modes("russian") == ["ask"]

    def test_russian_is_marked_as_stub(self):
        """«Спросить» по русскому должен честно отказывать, а не отвечать из истории."""
        assert subjects_cfg.is_stub("russian")
        assert not subjects_cfg.is_stub("history")

    def test_hidden_modes_are_available_but_not_visible(self):
        """«По темам» остаётся в коде, но из меню убран."""
        assert not subjects_cfg.is_mode_enabled("history", "topics")
        assert subjects_cfg.is_mode_available("history", "topics")

    def test_mistakes_mode_is_visible(self):
        assert subjects_cfg.is_mode_enabled("history", "mistakes")

    def test_russian_has_no_tests_at_all(self):
        assert not subjects_cfg.is_mode_available("russian", "tests")

    def test_only_history_accepts_uploads(self):
        assert subjects_cfg.teacher_can_upload("history", "tests")
        assert not subjects_cfg.teacher_can_upload("russian", "tests")


class TestMenuStructure:
    """Кабинет и режим ученика разделены, между ними есть переход в обе стороны."""

    def _texts(self, keyboard):
        return [b.text for row in keyboard.keyboard for b in row]

    def test_cabinet_has_only_setup_buttons(self):
        from bot.keyboards.inline import (
            MENU_BTN_ASK, MENU_BTN_STUDENT_VIEW, MENU_BTN_TESTS, teacher_cabinet_kb,
        )
        texts = self._texts(teacher_cabinet_kb("history"))
        assert MENU_BTN_TESTS not in texts
        assert MENU_BTN_ASK not in texts
        assert MENU_BTN_STUDENT_VIEW in texts

    def test_student_view_has_modes_and_way_back(self):
        from bot.keyboards.inline import (
            MENU_BTN_BACK_TO_CABINET, MENU_BTN_TESTS, MENU_BTN_UPLOAD, student_view_kb,
        )
        texts = self._texts(student_view_kb("history", for_teacher=True))
        assert MENU_BTN_TESTS in texts
        assert MENU_BTN_UPLOAD not in texts
        assert MENU_BTN_BACK_TO_CABINET in texts

    def test_student_never_sees_switch_buttons(self):
        from bot.keyboards.inline import (
            MENU_BTN_BACK_TO_CABINET, MENU_BTN_STUDENT_VIEW, main_menu_kb,
        )
        texts = self._texts(main_menu_kb("history", "student"))
        assert MENU_BTN_STUDENT_VIEW not in texts
        assert MENU_BTN_BACK_TO_CABINET not in texts

    def test_teacher_default_keyboard_is_cabinet(self):
        from bot.keyboards.inline import MENU_BTN_UPLOAD, main_menu_kb
        assert MENU_BTN_UPLOAD in self._texts(main_menu_kb("history", "teacher"))

    def test_every_button_can_break_out_of_waiting_states(self):
        """Любая кнопка меню обязана выводить из режима ожидания ввода.

        Режимы «жду файл», «жду ответ», «жду вопрос» перехватывают текст.
        Если подписи кнопки нет в этом наборе, нажатие уйдёт в обработчик
        режима — из теста будет не выйти, а нажатие уедет в модель.
        """
        from bot.keyboards.inline import (
            ALL_MENU_BUTTONS, main_menu_kb, student_view_kb, teacher_cabinet_kb,
        )
        keyboards = [
            teacher_cabinet_kb("history"),
            student_view_kb("history", for_teacher=True),
            main_menu_kb("history", "student"),
            main_menu_kb("russian", "student"),
        ]
        for keyboard in keyboards:
            missing = set(self._texts(keyboard)) - set(ALL_MENU_BUTTONS)
            assert not missing, f"кнопки без выхода из режима ожидания: {missing}"


class TestFlashcardsSource:
    def test_policy_is_base_for_history(self):
        assert subjects_cfg.mode_source("history", "flashcards") == subjects_cfg.SOURCE_BASE

    def test_teacher_cards_used_when_policy_switches(self, env, monkeypatch):
        """Ветка учительских карточек должна работать до появления загрузки."""
        monkeypatch.setitem(
            subjects_cfg.SUBJECTS["history"]["sources"],
            "flashcards",
            subjects_cfg.SOURCE_TEACHER,
        )
        storage.ensure_teacher_dirs(TEACHER, SUBJECT)
        storage.write_json(
            storage.teacher_flashcards_path(TEACHER, SUBJECT),
            {"dates": [{"Дата": "1569", "Событие": "Люблинская уния"}],
             "concepts": [], "persons": []},
        )

        cards = content_provider.get_flashcards(SUBJECT, teacher_id=TEACHER)
        assert len(cards["dates"]) == 1
        assert cards["dates"][0]["Дата"] == "1569"

    def test_falls_back_to_base_when_teacher_has_no_cards(self, env, monkeypatch):
        monkeypatch.setitem(
            subjects_cfg.SUBJECTS["history"]["sources"],
            "flashcards",
            subjects_cfg.SOURCE_TEACHER,
        )
        monkeypatch.setattr(
            content_provider.sheets_cache, "flash_dates",
            [{"Дата": "1410", "Событие": "Грюнвальд"}], raising=False,
        )
        storage.ensure_teacher_dirs(TEACHER, SUBJECT)

        cards = content_provider.get_flashcards(SUBJECT, teacher_id=TEACHER)
        assert cards["dates"][0]["Дата"] == "1410"

    def test_unknown_category_returns_empty(self, monkeypatch):
        monkeypatch.setattr(
            content_provider.sheets_cache, "flash_dates", [], raising=False
        )
        assert content_provider.get_flashcards_category("несуществующая") == []


class TestTeacherPreview:
    async def test_teacher_sees_own_trainer_without_extra_code(self, env):
        """Превью тренажёра — это обычный вход в тесты под своей ролью."""
        await db_module.ensure_user(TEACHER)
        await db_module.set_role(TEACHER, ROLE_TEACHER)
        await db_module.set_subject(TEACHER, SUBJECT)
        await db_module.ensure_teacher(TEACHER, SUBJECT)
        storage.ensure_teacher_dirs(TEACHER, SUBJECT)
        storage.write_json(
            storage.teacher_tests_path(TEACHER, SUBJECT),
            {"parser_version": 1, "rows": [{
                "Вариант": "мой", "Часть": "А", "№": "1",
                "Вопрос": "Проверочный вопрос преподавателя",
                "Вар.1": "Раз", "Вар.2": "Два", "Вар.3": "", "Вар.4": "", "Вар.5": "",
                "Ответ": "1", "Раздел": "",
            }]},
        )

        bundle = await content_provider.get_tests(TEACHER)
        assert len(bundle.rows) == 1
        assert bundle.teacher_id == TEACHER
        assert content_provider.variants_of(bundle.rows) == ["мой"]

    async def test_manifest_reports_rejected_count(self, env):
        """Преподаватель должен видеть, что часть вопросов не принята."""
        storage.ensure_teacher_dirs(TEACHER, SUBJECT)
        storage.write_json(
            storage.teacher_manifest_path(TEACHER, SUBJECT),
            {"parser_version": 1, "files": [{"filename": "a.docx", "accepted": 3}],
             "accepted_total": 3, "rejected_total": 7},
        )
        manifest = teacher_content.load_manifest(TEACHER, SUBJECT)
        assert manifest["rejected_total"] == 7
