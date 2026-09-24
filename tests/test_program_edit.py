"""Правка программы: скрыть стандартное, удалить своё (клиент, 24.09.2026).

Преподаватель перестроила программу на 52 темы в 8 разделах и хотела
убрать наши. Главные инварианты:

- скрытое не видно ученикам — ни в списках, ни в наборе вопросов;
- скрытие переживает пересборку тренажёра и /reparse;
- место с файлами преподавателя не пропадает молча;
- ключ удалённого своего раздела не достаётся новому.
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
from bot.handlers import sections_editor as editor  # noqa: E402
from services import content_provider, sections as sections_lib  # noqa: E402
from services import storage, teacher_content  # noqa: E402

SUBJECT = "history"
TEACHER, OTHER_TEACHER = 7401, 7402
STUDENT, OTHER_STUDENT = 7501, 7502

SAMPLE = ROOT / "data/raw/tests" / "Раннее Новое время.docx"


def _buttons(markup):
    return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]


@pytest.fixture
def env():
    tmpdir = Path(tempfile.mkdtemp(prefix="aida_program_"))
    saved = (storage.DATA_ROOT, storage.SUBJECTS_ROOT, storage.TEACHERS_ROOT)
    storage.DATA_ROOT = tmpdir / "data"
    storage.SUBJECTS_ROOT = storage.DATA_ROOT / "subjects"
    storage.TEACHERS_ROOT = storage.DATA_ROOT / "teachers"
    storage.ensure_teacher_dirs(TEACHER, SUBJECT)
    try:
        yield tmpdir
    finally:
        (storage.DATA_ROOT, storage.SUBJECTS_ROOT, storage.TEACHERS_ROOT) = saved
        shutil.rmtree(tmpdir, ignore_errors=True)


def _upload_sample(place: str) -> str:
    target, _, _ = teacher_content.store_upload(TEACHER, SUBJECT, SAMPLE, SAMPLE.name)
    teacher_content.rebuild(TEACHER, SUBJECT, {target.name: place})
    return target.name


needs_sample = pytest.mark.skipif(not SAMPLE.exists(), reason="нет образцов билетов")


class TestStorage:
    def test_hide_and_show(self, env):
        teacher_content.set_hidden(TEACHER, SUBJECT, ["5", "6_1"], True)
        assert teacher_content.hidden_places(TEACHER, SUBJECT) == {"5", "6_1"}
        teacher_content.set_hidden(TEACHER, SUBJECT, ["5"], False)
        assert teacher_content.hidden_places(TEACHER, SUBJECT) == {"6_1"}

    def test_own_places_are_deleted_not_hidden(self, env):
        key = teacher_content.add_custom_section(TEACHER, SUBJECT, "Религия")
        teacher_content.set_hidden(TEACHER, SUBJECT, [key], True)
        assert teacher_content.hidden_places(TEACHER, SUBJECT) == set()

    def test_hidden_topic_follows_its_section(self):
        assert sections_lib.is_hidden("6_3", {"6"})
        assert sections_lib.is_hidden("6_c1", {"6"})
        assert not sections_lib.is_hidden("6_3", {"6_1"})
        assert not sections_lib.is_hidden("16_1", {"1"})  # «1» — не начало строки
        assert not sections_lib.is_hidden("", {"6"})

    @needs_sample
    def test_rebuild_keeps_hidden(self, env):
        """/reparse пересобирает манифест — скрытое не должно вернуться."""
        _upload_sample("4")
        teacher_content.set_hidden(TEACHER, SUBJECT, ["5"], True)
        teacher_content.rebuild(TEACHER, SUBJECT)
        assert teacher_content.hidden_places(TEACHER, SUBJECT) == {"5"}

    def test_deleted_key_is_not_reused(self, env):
        first = teacher_content.add_custom_section(TEACHER, SUBJECT, "Первый")
        second = teacher_content.add_custom_section(TEACHER, SUBJECT, "Второй")
        assert teacher_content.delete_custom_place(TEACHER, SUBJECT, second)
        third = teacher_content.add_custom_section(TEACHER, SUBJECT, "Третий")
        assert third not in (first, second)

        topic = teacher_content.add_custom_topic(TEACHER, SUBJECT, "6", "Религия")
        teacher_content.delete_custom_place(TEACHER, SUBJECT, topic)
        assert teacher_content.add_custom_topic(TEACHER, SUBJECT, "6", "Политика") != topic

    def test_deleting_section_takes_its_topics(self, env):
        section = teacher_content.add_custom_section(TEACHER, SUBJECT, "Экономика")
        topic = teacher_content.add_custom_topic(TEACHER, SUBJECT, section, "Древность")
        teacher_content.delete_custom_place(TEACHER, SUBJECT, section)
        assert topic not in teacher_content.custom_topics(TEACHER, SUBJECT)

    def test_rename(self, env):
        key = teacher_content.add_custom_topic(TEACHER, SUBJECT, "6", "Релгия")
        assert teacher_content.rename_custom_place(TEACHER, SUBJECT, key, "Религия")
        assert teacher_content.custom_topics(TEACHER, SUBJECT)[key] == "Религия"
        # Стандартное не переименовывается: под новым названием остались бы
        # общие вопросы со старым смыслом
        assert not teacher_content.rename_custom_place(TEACHER, SUBJECT, "6_1", "Другое")


class TestScreen:
    def test_all_sections_listed_even_empty(self, env):
        """Раньше пустые разделы не показывались — а скрывать надо и пустые."""
        text, markup = editor.root_screen(TEACHER, SUBJECT)
        data = [d for _t, d in _buttons(markup)]
        for section in sections_lib.base_sections(SUBJECT):
            assert f"prg:s:{section.key}" in data
        assert "prg:new:-" in data
        assert "prg:hideall:-" in data

    def test_hidden_marked(self, env):
        teacher_content.set_hidden(TEACHER, SUBJECT, ["5", "6_1"], True)
        texts = [t for t, _d in _buttons(editor.root_screen(TEACHER, SUBJECT)[1])]
        assert any(t.startswith("🚫 5.") for t in texts)
        assert any(t.startswith("6.") and "скрыто тем: 1" in t for t in texts)
        assert any(t == "Вернуть все стандартные разделы" for t in texts)

        topic_texts = [t for t, _d in _buttons(editor.section_screen(TEACHER, SUBJECT, "6")[1])]
        assert any(t.startswith("🚫") for t in topic_texts)
        assert "Вернуть все стандартные темы" in topic_texts

    def test_hidden_section_screen_offers_return_only(self, env):
        teacher_content.set_hidden(TEACHER, SUBJECT, ["5"], True)
        text, markup = editor.section_screen(TEACHER, SUBJECT, "5")
        assert "скрыт" in text
        assert ("Вернуть раздел", "prg:show:5") in _buttons(markup)

    def test_own_section_can_be_renamed_and_deleted(self, env):
        key = teacher_content.add_custom_section(TEACHER, SUBJECT, "Религия по периодам")
        data = [d for _t, d in _buttons(editor.section_screen(TEACHER, SUBJECT, key)[1])]
        assert f"prg:ren:{key}" in data and f"prg:del:{key}" in data
        assert f"prg:hide:{key}" not in data

    def test_hide_all_topics(self, env):
        note = editor.hide_all(TEACHER, SUBJECT, "6")
        hidden = teacher_content.hidden_places(TEACHER, SUBJECT)
        assert {t.key for t in sections_lib.base_topics(SUBJECT, "6")} <= hidden
        assert "Скрыто тем" in note
        editor.show_all(TEACHER, SUBJECT, "6")
        assert teacher_content.hidden_places(TEACHER, SUBJECT) == set()

    @needs_sample
    def test_hide_all_skips_places_with_files(self, env):
        _upload_sample("4")
        note = editor.hide_all(TEACHER, SUBJECT, None)
        hidden = teacher_content.hidden_places(TEACHER, SUBJECT)
        assert "4" not in hidden
        assert "5" in hidden
        assert "лежат ваши файлы" in note

    @needs_sample
    def test_place_with_files_asks_where_to_move(self, env):
        name = _upload_sample("4")
        text, markup = editor.move_screen(TEACHER, SUBJECT, editor.ACTION_HIDE, "4")
        assert "переложить" in text
        data = [d for _t, d in _buttons(markup)]
        assert "prg:mv:h:4:5" in data
        assert "prg:mv:h:4:none" in data
        assert "prg:mv:h:4:4" not in data  # в само себя не перекладываем

        moved = teacher_content.move_place_files(TEACHER, SUBJECT, "4", "5")
        editor.remove_place(TEACHER, SUBJECT, editor.ACTION_HIDE, "4")
        assert moved == 1
        assert teacher_content.file_sections(TEACHER, SUBJECT)[name] == "5"
        assert "4" in teacher_content.hidden_places(TEACHER, SUBJECT)
        rows = teacher_content.load_tests(TEACHER, SUBJECT)
        assert rows and all(r["Раздел"] == "5" for r in rows)


class TestUploadPicker:
    def test_hidden_not_offered_on_upload(self, env):
        from bot.handlers.teacher_upload import _all_sections

        teacher_content.set_hidden(TEACHER, SUBJECT, ["5"], True)
        keys = [s.key for s in _all_sections(TEACHER, SUBJECT)]
        assert "5" not in keys
        assert "6" in keys


@pytest_asyncio.fixture
async def db_env(env):
    saved = db_module.DB_PATH
    db_module.DB_PATH = str(env / "test.sqlite3")
    try:
        await db_module.init_db()
        yield env
    finally:
        db_module.DB_PATH = saved


def _base_rows():
    def row(n, place):
        return {
            "Вариант": "", "Часть": "А", "№": str(n),
            "Вопрос": f"Общий вопрос номер {n} про историю, место {place}",
            "Вар.1": "Первый", "Вар.2": "Второй", "Вар.3": "", "Вар.4": "", "Вар.5": "",
            "Ответ": "1", "Раздел": place,
        }
    return [row(1, "5"), row(2, "5_2"), row(3, "6_1"), row(4, "6_2"), row(5, "")]


class TestTrainerScreen:
    """«Мой тренажёр» после правок 24.09.2026: части билета, без шума."""

    async def test_filled_trainer(self, db_env):
        from bot.handlers.menu import _trainer_screen

        rows = _base_rows()
        rows[0]["Часть"] = "В"
        rows[1]["Раздел"] = ""
        storage.write_json(
            storage.teacher_tests_path(TEACHER, SUBJECT),
            {"parser_version": 6, "updated_at": "2026-09-24T10:00:00", "rows": rows},
        )

        text, markup = await _trainer_screen(TEACHER, SUBJECT)

        assert "Часть А — 4" in text
        assert "Часть В — 1" in text
        assert sections_lib.UNSORTED_TITLE not in text
        data = [d for _t, d in _buttons(markup)]
        assert "trainer:sections" in data
        assert "trainer:files" not in data

    async def test_empty_trainer_keeps_sections_button(self, db_env):
        from bot.handlers.menu import _trainer_screen

        text, markup = await _trainer_screen(TEACHER, SUBJECT)
        assert "Пока пусто" in text
        assert "trainer:sections" in [d for _t, d in _buttons(markup)]


class TestStudentSees:
    async def _setup(self, monkeypatch):
        from database.models import ROLE_TEACHER

        monkeypatch.setattr(
            content_provider.sheets_cache, "tests_rows", _base_rows(), raising=False
        )
        for tid in (TEACHER, OTHER_TEACHER):
            await db_module.ensure_user(tid)
            await db_module.set_role(tid, ROLE_TEACHER)
            await db_module.set_subject(tid, SUBJECT)
            await db_module.ensure_teacher(tid, SUBJECT)
            storage.ensure_teacher_dirs(tid, SUBJECT)
        await db_module.ensure_user(STUDENT)
        await db_module.bind_student(STUDENT, TEACHER, SUBJECT)
        await db_module.ensure_user(OTHER_STUDENT)
        await db_module.bind_student(OTHER_STUDENT, OTHER_TEACHER, SUBJECT)

    async def test_hidden_base_questions_disappear(self, db_env, monkeypatch):
        await self._setup(monkeypatch)
        teacher_content.set_hidden(TEACHER, SUBJECT, ["5", "6_1"], True)

        places = {r["Раздел"] for r in (await content_provider.get_tests(STUDENT)).rows}
        assert places == {"6_2", ""}

    async def test_other_teachers_students_unaffected(self, db_env, monkeypatch):
        """Скрытие хранится у преподавателя — чужих учеников оно не касается."""
        await self._setup(monkeypatch)
        teacher_content.set_hidden(TEACHER, SUBJECT, ["5", "6_1"], True)

        rows = (await content_provider.get_tests(OTHER_STUDENT)).rows
        assert len(rows) == len(_base_rows())

    async def test_only_base_source_also_respects_hidden(self, db_env, monkeypatch):
        await self._setup(monkeypatch)
        await db_module.set_teacher_setting(
            TEACHER, SUBJECT, content_provider.MATERIALS_SOURCE, "base"
        )
        teacher_content.set_hidden(TEACHER, SUBJECT, ["6"], True)

        places = {r["Раздел"] for r in (await content_provider.get_tests(STUDENT)).rows}
        assert not any(sections_lib.section_of(p) == "6" for p in places)
