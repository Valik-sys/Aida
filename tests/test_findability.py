"""«Загрузил — и не нашёл»: где ученик найдёт вопросы и почему их не видно.

Список правок от 17.09.2026. Трое подряд загрузили вопросы и не нашли их,
хотя код показа работал:

- у одной стояли «только общие материалы», и загруженное не показывалось;
- у клиента тест лежал на уровень глубже — раздел «Обобщающие вопросы»,
  внутри тема с названием теста, — а на экране своего раздела вместо
  названия стояло «Раздел c1»;
- у третьего три файла молча уехали в тему, выбранную накануне.

Здесь проверяется, что бот теперь сам говорит, где искать, не прячет
от преподавателя его собственные пустые разделы и не переносит файлы молча.
"""

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
import subjects as subjects_cfg  # noqa: E402
from bot.handlers import teacher_upload as upload  # noqa: E402
from bot.handlers import tests as th  # noqa: E402
from bot.keyboards.inline import (  # noqa: E402
    BTN_BY_SECTION,
    SHOW_MINE_CALLBACK,
    move_file_kb,
    student_topics_kb,
    uploaded_kb,
    with_count,
)
from database.models import ROLE_TEACHER  # noqa: E402
from services import content_provider, storage, teacher_content  # noqa: E402
from services.content_provider import ContentBundle  # noqa: E402

SUBJECT = "history"
TEACHER = 9701
STUDENT = 9801


@pytest_asyncio.fixture
async def env():
    tmpdir = Path(tempfile.mkdtemp(prefix="aida_find_"))
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
        await db_module.ensure_user(TEACHER)
        await db_module.set_role(TEACHER, ROLE_TEACHER)
        await db_module.set_subject(TEACHER, SUBJECT)
        await db_module.ensure_teacher(TEACHER, SUBJECT)
        storage.ensure_teacher_dirs(TEACHER, SUBJECT)
        yield tmpdir
    finally:
        (
            db_module.DB_PATH,
            storage.DATA_ROOT, storage.SUBJECTS_ROOT, storage.TEACHERS_ROOT,
        ) = saved
        shutil.rmtree(tmpdir, ignore_errors=True)


def _row(section, i=0):
    return {
        "Вопрос": f"Вопрос номер {i} в месте {section}", "Ответ": "1",
        "Вар.1": "раз", "Вар.2": "два", "Раздел": section,
    }


def _bundle(rows):
    return ContentBundle(rows=rows, subject=SUBJECT, teacher_id=TEACHER)


def _andrey_layout():
    """Раскладка клиента: свой раздел, в нём две темы, одна пустая."""
    section = teacher_content.add_custom_section(TEACHER, SUBJECT, "Обобщающие вопросы")
    empty = teacher_content.add_custom_topic(TEACHER, SUBJECT, section, "1")
    full = teacher_content.add_custom_topic(
        TEACHER, SUBJECT, section, "10 класс обобщение по 2 разделу"
    )
    return section, empty, full


class TestSectionHeading:
    """Заголовок своего раздела — его название, а не «Раздел c1»."""

    def test_custom_section_shows_its_name(self, env):
        section, _empty, full = _andrey_layout()
        heading = th._section_heading(_bundle([_row(full)]), section)

        assert heading == "Обобщающие вопросы"
        assert "c1" not in heading

    def test_programme_section_shows_full_title(self, env):
        heading = th._section_heading(_bundle([_row("1_1")]), "1")
        assert heading.startswith("1. Становление древнейших цивилизаций")


class TestCountsNextToSections:
    def test_section_label_carries_the_count(self, env):
        section, _empty, full = _andrey_layout()
        rows = [_row(full, i) for i in range(75)] + [_row("1_1", i) for i in range(12)]

        listed = dict(th._available_sections(_bundle(rows)))

        assert listed[section] == "Обобщающие вопросы · 75"
        # Короткое название: полное на телефоне обрезалось вместе с числом
        assert listed["1"] == "1. Древнейшие цивилизации · 12"

    def test_topic_label_carries_the_count(self):
        kb = student_topics_kb([("c1_c2", "10 класс обобщение по 2 разделу", 75)], "c1")
        assert kb.inline_keyboard[0][0].text.endswith("· 75")


class TestOwnEmptyPlaces:
    """Пустой свой раздел видит преподаватель, но не ученик."""

    def test_student_does_not_see_empty_topic(self, env):
        section, empty, full = _andrey_layout()
        topics = th._available_topics(_bundle([_row(full)]), section)

        assert [key for key, _t, _n in topics] == [full]

    def test_teacher_sees_his_empty_topic(self, env):
        section, empty, full = _andrey_layout()
        topics = th._available_topics(_bundle([_row(full)]), section, owner=True)

        assert [(key, n) for key, _t, n in topics] == [(empty, 0), (full, 1)]

    def test_teacher_sees_empty_section(self, env):
        section = teacher_content.add_custom_section(TEACHER, SUBJECT, "Тест")
        listed = dict(th._available_sections(_bundle([_row("1_1")]), owner=True))

        assert listed[section] == "Тест · пока пусто"

    def test_student_does_not_see_empty_section(self, env):
        section = teacher_content.add_custom_section(TEACHER, SUBJECT, "Тест")
        listed = dict(th._available_sections(_bundle([_row("1_1")])))

        assert section not in listed

    def test_empty_own_topic_in_programme_section_shows_the_section(self, env):
        """Своя тема в разделе программы: пустая — но раздел должен быть виден,
        иначе до неё не добраться."""
        topic = teacher_content.add_custom_topic(TEACHER, SUBJECT, "4", "Моя тема")
        listed = dict(th._available_sections(_bundle([_row("1_1")]), owner=True))

        assert listed["4"] == "4. Раннее Новое время · пока пусто"
        topics = th._available_topics(_bundle([_row("1_1")]), "4", owner=True)
        assert [key for key, _t, _n in topics] == [topic]

    def test_owner_is_the_teacher_only(self):
        bundle = _bundle([])
        assert th._is_owner(bundle, TEACHER) is True
        assert th._is_owner(bundle, STUDENT) is False

    def test_empty_mark_on_button(self):
        assert with_count("Тест", 0) == "Тест · пока пусто"


class TestStudentPath:
    """Отчёт о загрузке называет путь теми же подписями, что на кнопках."""

    def test_path_to_own_topic(self, env):
        section, _empty, full = _andrey_layout()
        path = upload.student_path(TEACHER, SUBJECT, full)

        assert path == (
            f"{subjects_cfg.MODE_LABELS['tests']} → {BTN_BY_SECTION} → "
            "Обобщающие вопросы → 10 класс обобщение по 2 разделу"
        )

    def test_path_to_programme_section(self, env):
        path = upload.student_path(TEACHER, SUBJECT, "6")
        assert path.endswith(f"{BTN_BY_SECTION} → 6. 1917–1945")

    def test_path_to_mixed_questions(self, env):
        path = upload.student_path(TEACHER, SUBJECT, "")
        assert path.endswith("Смешанные вопросы")

    def test_path_matches_student_button(self, env):
        """Подпись раздела в пути — ровно та, что на кнопке у ученика."""
        section, _empty, full = _andrey_layout()
        button = dict(th._available_sections(_bundle([_row(full)])))[section]
        path = upload.student_path(TEACHER, SUBJECT, full)

        assert button.split(" · ")[0] in path


class TestHiddenWarning:
    async def test_base_only_hides_uploads(self, env):
        await db_module.set_teacher_setting(
            TEACHER, SUBJECT, content_provider.MATERIALS_SOURCE, subjects_cfg.SOURCE_BASE
        )
        text, hidden = await upload._where_to_find(TEACHER, SUBJECT, "1")

        assert hidden is True
        assert upload.HIDDEN_WARNING in text

    async def test_own_materials_are_visible(self, env):
        await db_module.set_teacher_setting(
            TEACHER, SUBJECT, content_provider.MATERIALS_SOURCE, subjects_cfg.SOURCE_TEACHER
        )
        text, hidden = await upload._where_to_find(TEACHER, SUBJECT, "1")

        assert hidden is False
        assert upload.HIDDEN_WARNING not in text

    def test_button_appears_only_when_hidden(self):
        assert _callbacks(uploaded_kb("1", "tok", show_mine=True))[0] == SHOW_MINE_CALLBACK
        assert SHOW_MINE_CALLBACK not in _callbacks(uploaded_kb("1", "tok"))

    def test_button_is_removed_after_click(self):
        kb = uploaded_kb("1", "tok", show_mine=True)
        stripped = upload._without_show_mine(kb)

        assert SHOW_MINE_CALLBACK not in _callbacks(stripped)
        assert len(stripped.inline_keyboard) == len(kb.inline_keyboard) - 1


class TestStickySection:
    """Выбранный раздел держится полчаса, а не до следующего дня."""

    def test_fresh_choice_holds(self):
        just_now = (upload._utcnow() - dt.timedelta(minutes=5)).isoformat()
        assert upload._is_fresh(just_now) is True

    def test_yesterday_choice_does_not(self):
        yesterday = (upload._utcnow() - dt.timedelta(days=1)).isoformat()
        assert upload._is_fresh(yesterday) is False

    def test_old_sessions_without_time_do_not_hold(self):
        # Так выглядят сессии, начатые до этой правки
        assert upload._is_fresh(None) is False
        assert upload._is_fresh("") is False


class TestMovingSameFile:
    def test_move_buttons_fit_telegram_limit(self):
        """В callback-данных не больше 64 байт, даже у длинного ключа темы."""
        token = teacher_content.file_token("очень длинное имя файла с пробником.docx")
        kb = move_file_kb(token, "6_9_10", "Раздел", "Другой раздел", show_mine=True)

        for row in kb.inline_keyboard:
            for button in row:
                assert len(button.callback_data.encode("utf-8")) <= 64

    def test_move_to_mixed_questions_keeps_empty_key(self):
        kb = move_file_kb("tok", "", "Смешанные вопросы", "Тест")
        move = kb.inline_keyboard[0][0].callback_data
        assert move == "upl:move:tok:"

    def test_upload_name_is_known_before_saving(self, env):
        """Имя, под которым ляжет файл, известно заранее — по нему и
        узнаётся, что такой файл уже лежит в другом месте."""
        src = env / "incoming.pdf"
        src.write_bytes(b"%PDF-1.4 test")
        expected = teacher_content.upload_name("Пробник 1.pdf")
        stored, *_ = teacher_content.store_upload(TEACHER, SUBJECT, src, "Пробник 1.pdf")

        assert stored.name == expected


def _callbacks(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]
