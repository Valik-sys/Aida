"""Разделы преподавателя: раскладка файлов и подсказка по имени.

Главный инвариант здесь — раскладка переживает пересборку. `tests.json`
собирается из папки `uploads/` заново при каждой загрузке и при `/reparse`,
и если раздел потеряется, преподаватель разложит материалы один раз, а
после ближайшего улучшения парсера обнаружит всё в одной куче.
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

import subjects as subjects_cfg  # noqa: E402
from services import sections as sections_lib  # noqa: E402
from services import storage, teacher_content  # noqa: E402

SAMPLES = ROOT / "data/raw/tests"
SUBJECT = "history"
TEACHER = 7301

SAMPLE_ONE = "Раннее Новое время.docx"
SAMPLE_TWO = "1. 1918-1945 гг..docx"

pytestmark = pytest.mark.skipif(
    not (SAMPLES / SAMPLE_ONE).exists(), reason="нет образцов билетов"
)


@pytest.fixture
def env():
    tmpdir = Path(tempfile.mkdtemp(prefix="aida_sections_"))
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


def _upload(name: str) -> str:
    target, _, _ = teacher_content.store_upload(TEACHER, SUBJECT, SAMPLES / name, name)
    return target.name


class TestSuggestion:
    """Подсказка раздела по имени файла."""

    def test_years_pick_the_period(self):
        assert sections_lib.suggest("1. 1918-1945 гг.docx", SUBJECT) == "6"

    def test_roman_century_is_understood(self):
        assert sections_lib.suggest("XIX_начало_XX_века.docx", SUBJECT) == "5"

    def test_cyrillic_lookalikes_count_as_roman(self):
        # «ХІХ» здесь набрано кириллицей — на глаз не отличить, и такие
        # имена файлов встречаются наравне с латинскими
        assert sections_lib.suggest("Беларусь у ХІХ ст..docx", SUBJECT) == "5"

    def test_keywords_work_without_dates(self):
        assert sections_lib.suggest("Древнейшие цивилизации 1.docx", SUBJECT) == "1"
        assert sections_lib.suggest("Военный коммунизм и НЭП.docx", SUBJECT) == "6"

    def test_transliterated_names_still_match(self):
        name = "3_2_Sotsialno_politicheskoe_razvitie_srednevekovykh_knyazhestv.docx"
        assert sections_lib.suggest(name, SUBJECT) == "2"

    def test_silent_when_unclear(self):
        # Молчать лучше, чем подсказать наугад: подтверждённая не глядя
        # подсказка портит материалы тише, чем лишний вопрос
        assert sections_lib.suggest("Обобщение 4.1—5.2 А.docx", SUBJECT) is None
        assert sections_lib.suggest("конспект РБ.docx", SUBJECT) is None

    def test_silent_on_a_tie(self):
        # Файл на стыке периодов: XIX век и 1945 год тянут в разные разделы
        assert sections_lib.suggest("2 XIX век-1945 гг.docx", SUBJECT) is None

    def test_subject_without_sections(self):
        assert sections_lib.suggest("Что угодно.docx", "russian") is None


class TestAssignment:
    """Раскладка файлов по разделам."""

    def test_section_reaches_every_row(self, env):
        name = _upload(SAMPLE_ONE)
        result = teacher_content.rebuild(TEACHER, SUBJECT, {name: "4"})

        assert result.total_accepted > 0
        assert {r["Раздел"] for r in result.rows} == {"4"}

    def test_survives_reparse(self, env):
        """Тот самый инвариант: пересборка не теряет раскладку."""
        name = _upload(SAMPLE_ONE)
        teacher_content.rebuild(TEACHER, SUBJECT, {name: "4"})

        # Так работает /reparse — без каких-либо подсказок снаружи
        again = teacher_content.rebuild(TEACHER, SUBJECT)

        assert {r["Раздел"] for r in again.rows} == {"4"}
        assert teacher_content.file_sections(TEACHER, SUBJECT)[name] == "4"

    def test_new_file_does_not_disturb_the_old_one(self, env):
        first = _upload(SAMPLE_ONE)
        teacher_content.rebuild(TEACHER, SUBJECT, {first: "4"})

        second = _upload(SAMPLE_TWO)
        result = teacher_content.rebuild(TEACHER, SUBJECT, {second: "6"})

        by_variant = {}
        for row in result.rows:
            by_variant.setdefault(row["Вариант"], set()).add(row["Раздел"])
        assert by_variant[Path(first).stem] == {"4"}
        assert by_variant[Path(second).stem] == {"6"}

    def test_file_without_section_stays_empty(self, env):
        name = _upload(SAMPLE_ONE)
        result = teacher_content.rebuild(TEACHER, SUBJECT)

        assert {r["Раздел"] for r in result.rows} == {""}
        assert teacher_content.files_without_section(TEACHER, SUBJECT) == [name]

    def test_moving_a_file_rewrites_rows(self, env):
        name = _upload(SAMPLE_ONE)
        teacher_content.rebuild(TEACHER, SUBJECT, {name: "4"})

        assert teacher_content.set_file_section(TEACHER, SUBJECT, name, "3") is True

        rows = teacher_content.load_tests(TEACHER, SUBJECT)
        assert {r["Раздел"] for r in rows} == {"3"}
        assert teacher_content.files_without_section(TEACHER, SUBJECT) == []

    def test_moving_an_unknown_file_is_refused(self, env):
        _upload(SAMPLE_ONE)
        teacher_content.rebuild(TEACHER, SUBJECT)
        assert teacher_content.set_file_section(TEACHER, SUBJECT, "нет.docx", "3") is False

    def test_counts_by_section(self, env):
        first = _upload(SAMPLE_ONE)
        second = _upload(SAMPLE_TWO)
        teacher_content.rebuild(TEACHER, SUBJECT, {first: "4"})

        counts = teacher_content.counts_by_section(TEACHER, SUBJECT)
        assert counts["4"] > 0
        assert counts[""] > 0  # второй файл раздела не получил


class TestCustomSections:
    """Свои разделы преподавателя."""

    def test_added_and_listed(self, env):
        _upload(SAMPLE_ONE)
        teacher_content.rebuild(TEACHER, SUBJECT)

        key = teacher_content.add_custom_section(TEACHER, SUBJECT, "Обобщение")
        assert key == "c1"

        custom = teacher_content.custom_sections(TEACHER, SUBJECT)
        assert custom == {"c1": "Обобщение"}
        assert sections_lib.title_of(SUBJECT, "c1", custom) == "Обобщение"

    def test_same_name_reuses_the_key(self, env):
        first = teacher_content.add_custom_section(TEACHER, SUBJECT, "Обобщение")
        second = teacher_content.add_custom_section(TEACHER, SUBJECT, "  обобщение ")
        assert first == second
        assert len(teacher_content.custom_sections(TEACHER, SUBJECT)) == 1

    def test_empty_name_refused(self, env):
        assert teacher_content.add_custom_section(TEACHER, SUBJECT, "   ") is None

    def test_survives_rebuild(self, env):
        name = _upload(SAMPLE_ONE)
        teacher_content.rebuild(TEACHER, SUBJECT)
        key = teacher_content.add_custom_section(TEACHER, SUBJECT, "Обобщение")
        teacher_content.set_file_section(TEACHER, SUBJECT, name, key)

        teacher_content.rebuild(TEACHER, SUBJECT)

        assert teacher_content.custom_sections(TEACHER, SUBJECT) == {key: "Обобщение"}
        assert {r["Раздел"] for r in teacher_content.load_tests(TEACHER, SUBJECT)} == {key}

    def test_valid_only_for_existing_keys(self, env):
        custom = {"c1": "Обобщение"}
        assert sections_lib.is_valid(SUBJECT, "c1", custom)
        assert sections_lib.is_valid(SUBJECT, "6", custom)
        assert sections_lib.is_valid(SUBJECT, "", custom)      # «без раздела» — тоже выбор
        assert not sections_lib.is_valid(SUBJECT, "c9", custom)
        assert not sections_lib.is_valid(SUBJECT, "99", custom)


class TestScreens:
    """Экран «Разделы» и кнопки выбора."""

    def test_lists_only_filled_sections(self, env):
        from bot.handlers.menu import _sections_text

        name = _upload(SAMPLE_ONE)
        teacher_content.rebuild(TEACHER, SUBJECT, {name: "4"})

        text, has_unsorted = _sections_text(TEACHER, SUBJECT)

        assert "4. Раннее Новое время" in text
        assert "1. Древнейшие цивилизации" not in text  # пустые не показываем
        assert has_unsorted is False

    def test_unsorted_shown_as_mixed_questions(self, env):
        from bot.handlers.menu import _sections_text

        _upload(SAMPLE_ONE)
        teacher_content.rebuild(TEACHER, SUBJECT)

        text, has_unsorted = _sections_text(TEACHER, SUBJECT)

        # Одно название и у преподавателя, и у ученика
        assert sections_lib.UNSORTED_TITLE in text
        assert has_unsorted is True

    def test_empty_trainer(self, env):
        from bot.handlers.menu import _sections_text

        text, has_unsorted = _sections_text(TEACHER, SUBJECT)
        assert "Пока ничего не загружено" in text
        assert has_unsorted is False

    def test_upload_keyboard_carries_section_keys(self):
        from bot.keyboards.inline import SEC_PREFIX, sections_kb_for_upload

        kb = sections_kb_for_upload(sections_lib.merged(SUBJECT, {"c1": "Обобщение"}))
        data = [b.callback_data for row in kb.inline_keyboard for b in row]

        assert f"{SEC_PREFIX}:6" in data
        assert f"{SEC_PREFIX}:c1" in data
        assert f"{SEC_PREFIX}:new" in data
        assert f"{SEC_PREFIX}:none" in data

    def test_move_button_points_at_its_own_file(self, env):
        from bot.keyboards.inline import SEC_PREFIX, uploaded_kb

        first = _upload(SAMPLE_ONE)
        second = _upload(SAMPLE_TWO)
        teacher_content.rebuild(TEACHER, SUBJECT, {first: "4", second: "6"})

        token = teacher_content.file_token(first)
        kb = uploaded_kb("4. Раннее Новое время", token)
        data = [b.callback_data for row in kb.inline_keyboard for b in row]

        # Кнопка под отчётом первого файла должна вести к первому файлу,
        # даже когда после него загрузили второй
        assert f"{SEC_PREFIX}:list:{token}" in data
        assert all(len(d.encode()) <= 64 for d in data)
        assert teacher_content.find_file_by_token(TEACHER, SUBJECT, token) == first

    def test_unknown_file_token(self, env):
        _upload(SAMPLE_ONE)
        teacher_content.rebuild(TEACHER, SUBJECT)

        assert teacher_content.find_file_by_token(TEACHER, SUBJECT, "нетакого") is None
        assert teacher_content.find_file_by_token(TEACHER, SUBJECT, "") is None

    def test_no_move_button_without_a_section(self):
        from bot.keyboards.inline import uploaded_kb

        # Файл без раздела перекладывать некуда — кнопки быть не должно
        data = [
            b.callback_data
            for row in uploaded_kb().inline_keyboard
            for b in row
        ]
        assert data == ["trainer:root"]

    def test_confirm_keyboard_offers_a_way_out(self):
        from bot.keyboards.inline import SEC_PREFIX, section_confirm_kb

        kb = section_confirm_kb("6", "6. 1917–1945")
        data = [b.callback_data for row in kb.inline_keyboard for b in row]

        # Подсказку можно принять, поменять и отказаться от неё
        assert data == [f"{SEC_PREFIX}:6", f"{SEC_PREFIX}:list", f"{SEC_PREFIX}:none"]


class _Stub:
    """Заглушки сообщения и нажатия: собирают то, что бот отправил."""

    class Chat:
        def __init__(self, cid):
            self.id = cid

    class User:
        def __init__(self, uid):
            self.id = uid

    class Message:
        def __init__(self, text=None, uid=TEACHER):
            self.text = text
            self.from_user = _Stub.User(uid)
            self.chat = _Stub.Chat(uid)
            self.sent = []

        async def answer(self, text, reply_markup=None, **kw):
            self.sent.append(text)
            _Stub.log.append(text)
            return self

        async def edit_text(self, text, reply_markup=None, **kw):
            """Мастер заменяет свой прошлый экран, а не шлёт новый."""
            self.sent.append(text)
            _Stub.log.append(text)
            return self

    class Callback:
        def __init__(self, data, uid=TEACHER):
            self.data = data
            self.from_user = _Stub.User(uid)
            self.message = _Stub.Message(uid=uid)

        async def answer(self, *a, **kw):
            return None

    log: list = []


class TestSortingFlow:
    """Разбор старых загрузок: пропуск и нажатия по остывшим кнопкам."""

    @pytest_asyncio.fixture
    async def flow(self, env):
        """Преподаватель с двумя файлами без разделов и живым состоянием FSM."""
        from aiogram.fsm.context import FSMContext
        from aiogram.fsm.storage.base import StorageKey
        from aiogram.fsm.storage.memory import MemoryStorage

        import bot.handlers.teacher_upload as tu
        import database.db as db_module
        from database.models import ROLE_TEACHER

        saved_db = db_module.DB_PATH
        db_module.DB_PATH = str(env / "flow.sqlite3")
        try:
            await db_module.init_db()
            await db_module.ensure_user(TEACHER)
            await db_module.set_role(TEACHER, ROLE_TEACHER)
            await db_module.set_subject(TEACHER, SUBJECT)

            _upload(SAMPLE_ONE)
            _upload(SAMPLE_TWO)
            teacher_content.rebuild(TEACHER, SUBJECT)

            _Stub.log.clear()
            yield tu, FSMContext(
                storage=MemoryStorage(), key=StorageKey(1, TEACHER, TEACHER)
            )
        finally:
            db_module.DB_PATH = saved_db

    async def test_mixed_questions_moves_to_the_next_file(self, flow):
        tu, state = flow

        await tu.sort_entry(_Stub.Callback("trainer:sort"), state)
        first = (await state.get_data())["sec_target"]

        await tu.section_chosen(_Stub.Callback(f"{tu.SEC_PREFIX}:{tu.UNSORTED_KEY}"), state)
        second = (await state.get_data())["sec_target"]

        # Раздел у файла так и не появился, но по кругу он не возвращается
        assert second and second != first
        assert any(tu.UNSORTED_LABEL in text for text in _Stub.log)

    async def test_marking_everything_ends(self, flow):
        tu, state = flow

        await tu.sort_entry(_Stub.Callback("trainer:sort"), state)
        for _ in range(2):
            await tu.section_chosen(
                _Stub.Callback(f"{tu.SEC_PREFIX}:{tu.UNSORTED_KEY}"), state
            )

        assert (await state.get_data()).get("sec_target") is None
        # Итог показывается вместе с судьбой последнего файла, одним экраном
        assert any("Готово, все файлы разложены." in text for text in _Stub.log)

    async def test_stale_button_does_not_pretend(self, flow):
        tu, state = flow

        # Разложили всё, а потом нажали кнопку в старом сообщении.
        # Раскладка идёт в два нажатия: раздел, затем тема.
        await tu.sort_entry(_Stub.Callback("trainer:sort"), state)
        for key in ("6", "4"):
            await tu.section_chosen(_Stub.Callback(f"{tu.SEC_PREFIX}:{key}"), state)
            await tu.topic_chosen(
                _Stub.Callback(f"{tu.TOP_PREFIX}:{tu.WHOLE_SECTION_KEY}"), state
            )
        _Stub.log.clear()

        await tu.section_chosen(_Stub.Callback(f"{tu.SEC_PREFIX}:4"), state)

        assert any("уже разложен" in text for text in _Stub.log)
        assert not any("None" in text for text in _Stub.log)


class TestStudentFiltering:
    """Как раздел преподавателя доходит до тренировки ученика."""

    def test_programme_section_matches(self):
        from bot.handlers.tests import _match_section

        assert _match_section("6", 6) is True
        assert _match_section("Раздел 6", 6) is True
        assert _match_section("", 6) is False

    def test_custom_section_does_not_leak_into_programme(self):
        from bot.handlers.tests import _match_section

        # «c1» содержит цифру 1, и по вхождению попадал бы в первый раздел
        assert _match_section("c1", 1) is False
        assert _match_section("c2", 2) is False

    def test_custom_section_matches_itself(self):
        from bot.handlers.tests import _match_section

        assert _match_section("c1", "c1") is True
        assert _match_section("c1", "c2") is False

    def test_empty_key_selects_unsorted(self):
        from bot.handlers.tests import _match_section

        # «Смешанные вопросы» — это ровно те, которым раздел не назначен
        assert _match_section("", "") is True
        assert _match_section("6", "") is False


class TestStudentSectionList:
    """Список разделов, который видит ученик."""

    def _bundle(self, rows, teacher_id=TEACHER):
        from services.content_provider import ContentBundle

        return ContentBundle(rows=rows, subject=SUBJECT, teacher_id=teacher_id)

    def _row(self, section):
        return {"Вопрос": "?", "Ответ": "1", "Раздел": section}

    def test_shows_only_sections_with_questions(self, env):
        from bot.handlers.tests import _available_sections

        listed = _available_sections(
            self._bundle([self._row("6"), self._row("6"), self._row("1")])
        )
        keys = [key for key, _ in listed]

        assert keys == ["1", "6"]  # остальные семь не показываем

    def test_custom_section_is_offered(self, env):
        from bot.handlers.tests import _available_sections

        teacher_content.add_custom_section(TEACHER, SUBJECT, "Обобщение")
        listed = _available_sections(self._bundle([self._row("c1")]))

        assert listed == [("c1", "Обобщение")]

    def test_unsorted_becomes_mixed_questions(self, env):
        from bot.handlers.tests import UNSORTED_KEY, _available_sections

        listed = _available_sections(self._bundle([self._row("6"), self._row("")]))
        keys = [key for key, _ in listed]

        # Полные билеты без раздела должны быть доступны, а не потеряны
        assert keys == ["6", UNSORTED_KEY]

    def test_nothing_to_show(self, env):
        from bot.handlers.tests import _available_sections

        assert _available_sections(self._bundle([])) == []

    def test_works_without_teacher(self, env):
        from bot.handlers.tests import _available_sections

        listed = _available_sections(self._bundle([self._row("3")], teacher_id=None))
        assert [key for key, _ in listed] == ["3"]


class TestConfig:
    """Сетка разделов приходит из конфига предмета."""

    def test_history_has_the_programme(self):
        keys = [s.key for s in sections_lib.base_sections(SUBJECT)]
        assert keys == [str(n) for n in range(1, 9)]

    def test_subject_without_sections(self):
        assert sections_lib.base_sections("russian") == []
        assert not subjects_cfg.has_sections("russian")

    def test_section_names_come_from_config(self):
        from bot.keyboards.inline import SECTION_NAMES

        assert len(SECTION_NAMES) == 8
        assert SECTION_NAMES[5].startswith("6. ")
