"""Темы — второй уровень внутри раздела.

Главное, что здесь проверяется: **ключ темы несёт в себе раздел**. Из этого
следует всё остальное — раздел восстанавливается из темы без отдельного поля,
старые материалы, разложенные до появления тем, продолжают работать, а выбор
раздела у ученика вбирает и то, что разложено по темам внутри него.

Второй инвариант тот же, что у разделов: раскладка живёт в манифесте и
переживает пересборку `tests.json` из `uploads/`.
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
TEACHER = 7401
STUDENT = 7402

SAMPLE_ONE = "Раннее Новое время.docx"
SAMPLE_TWO = "1. 1918-1945 гг..docx"

pytestmark = pytest.mark.skipif(
    not (SAMPLES / SAMPLE_ONE).exists(), reason="нет образцов билетов"
)


@pytest.fixture
def env():
    tmpdir = Path(tempfile.mkdtemp(prefix="aida_topics_"))
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


class TestKeys:
    """Ключ темы и раздел, который из него читается."""

    def test_section_is_read_back_from_topic(self):
        assert sections_lib.section_of("6_1") == "6"
        assert sections_lib.section_of("6_9_10") == "6"
        assert sections_lib.section_of("6") == "6"
        assert sections_lib.section_of("") == ""

    def test_custom_section_keeps_its_key(self):
        # Своя тема в своём разделе: «c1_c1» лежит в разделе «c1»
        assert sections_lib.section_of("c1_c1") == "c1"

    def test_topic_is_distinguished_from_section(self):
        assert sections_lib.is_topic("6_1")
        assert not sections_lib.is_topic("6")
        assert not sections_lib.is_topic("")

    def test_grid_covers_every_section(self):
        for section in sections_lib.base_sections(SUBJECT):
            assert sections_lib.base_topics(SUBJECT, section.key), section.key

    def test_topic_keys_start_with_their_section(self):
        """Иначе раздел из темы прочитается неверно и вопросы уедут к соседу."""
        for section in sections_lib.base_sections(SUBJECT):
            for topic in sections_lib.base_topics(SUBJECT, section.key):
                assert sections_lib.section_of(topic.key) == section.key

    def test_topic_codes_match_base_content(self):
        """Коды тем совпадают с «Тема_код» базового контента и вкладками теории.

        Расхождение ничего не сломает громко — просто перестанет находиться
        теория, и заметят это не скоро.
        """
        from bot.handlers import topics as topics_handler

        for section, codes in topics_handler.SUBTOPIC_CODES.items():
            assert [t.key for t in sections_lib.base_topics(SUBJECT, section)] == codes


class TestValidation:
    """Что считается существующей темой."""

    def test_topic_of_its_own_section_is_valid(self):
        assert sections_lib.is_valid(SUBJECT, "6_1")

    def test_topic_of_another_section_is_rejected(self):
        # «6_1» существует, но в разделе 6 — а ключ утверждает раздел 7
        assert not sections_lib.is_valid(SUBJECT, "7_1_6")

    def test_invented_topic_is_rejected(self):
        assert not sections_lib.is_valid(SUBJECT, "6_99")

    def test_custom_topic_is_valid_only_with_manifest(self):
        assert not sections_lib.is_valid(SUBJECT, "6_c1")
        assert sections_lib.is_valid(SUBJECT, "6_c1", None, {"6_c1": "Своя тема"})


class TestCustomTopics:
    """Свои темы преподавателя."""

    def test_added_topic_gets_key_inside_its_section(self, env):
        key = teacher_content.add_custom_topic(TEACHER, SUBJECT, "6", "Партизаны")
        assert sections_lib.section_of(key) == "6"
        assert teacher_content.custom_topics(TEACHER, SUBJECT)[key] == "Партизаны"

    def test_same_title_does_not_multiply(self, env):
        first = teacher_content.add_custom_topic(TEACHER, SUBJECT, "6", "Партизаны")
        second = teacher_content.add_custom_topic(TEACHER, SUBJECT, "6", "партизаны")
        assert first == second

    def test_same_title_in_another_section_is_another_topic(self, env):
        first = teacher_content.add_custom_topic(TEACHER, SUBJECT, "6", "Культура")
        second = teacher_content.add_custom_topic(TEACHER, SUBJECT, "7", "Культура")
        assert first != second
        assert sections_lib.section_of(second) == "7"

    def test_empty_title_is_refused(self, env):
        assert teacher_content.add_custom_topic(TEACHER, SUBJECT, "6", "   ") is None

    def test_own_topics_appear_next_to_programme_ones(self, env):
        key = teacher_content.add_custom_topic(TEACHER, SUBJECT, "6", "Партизаны")
        custom = teacher_content.custom_topics(TEACHER, SUBJECT)
        merged = sections_lib.merged_topics(SUBJECT, "6", custom)

        assert key in [t.key for t in merged]
        # Чужие темы в этот раздел не подмешиваются
        assert all(sections_lib.section_of(t.key) == "6" for t in merged)

    def test_topics_survive_rebuild(self, env):
        """Пересборка из uploads/ не должна стирать список своих тем."""
        _upload(SAMPLE_ONE)
        key = teacher_content.add_custom_topic(TEACHER, SUBJECT, "4", "Своя тема")
        teacher_content.rebuild(TEACHER, SUBJECT)

        assert teacher_content.custom_topics(TEACHER, SUBJECT).get(key) == "Своя тема"


class TestFilePlacement:
    """Файл, положенный в тему."""

    def test_topic_reaches_every_row_of_the_file(self, env):
        name = _upload(SAMPLE_ONE)
        teacher_content.rebuild(TEACHER, SUBJECT, assign={name: "4_6"})

        rows = teacher_content.load_tests(TEACHER, SUBJECT)
        assert rows
        assert {row["Раздел"] for row in rows} == {"4_6"}

    def test_placement_survives_rebuild(self, env):
        """Тот же инвариант, что у разделов: раскладка живёт в манифесте."""
        name = _upload(SAMPLE_ONE)
        teacher_content.rebuild(TEACHER, SUBJECT, assign={name: "4_6"})
        teacher_content.rebuild(TEACHER, SUBJECT)

        rows = teacher_content.load_tests(TEACHER, SUBJECT)
        assert {row["Раздел"] for row in rows} == {"4_6"}

    def test_moving_file_to_topic_does_not_reparse(self, env):
        name = _upload(SAMPLE_ONE)
        teacher_content.rebuild(TEACHER, SUBJECT, assign={name: "4"})

        assert teacher_content.set_file_section(TEACHER, SUBJECT, name, "4_6")

        rows = teacher_content.load_tests(TEACHER, SUBJECT)
        assert {row["Раздел"] for row in rows} == {"4_6"}

    def test_files_in_different_topics_do_not_mix(self, env):
        one = _upload(SAMPLE_ONE)
        two = _upload(SAMPLE_TWO)
        teacher_content.rebuild(
            TEACHER, SUBJECT, assign={one: "4_6", two: "6_9_10"}
        )

        rows = teacher_content.load_tests(TEACHER, SUBJECT)
        assert {row["Раздел"] for row in rows} == {"4_6", "6_9_10"}


class TestLabels:
    """Как место называется на экране."""

    def test_place_shows_section_and_topic(self):
        title = sections_lib.place_title(SUBJECT, "6_8")
        assert "6." in title
        assert "Вторая мировая война" in title
        assert "→" in title

    def test_place_of_section_has_no_arrow(self):
        assert "→" not in sections_lib.place_title(SUBJECT, "6")

    def test_place_of_nothing_is_the_mixed_pile(self):
        assert sections_lib.place_title(SUBJECT, "") == sections_lib.UNSORTED_TITLE

    def test_custom_topic_is_named_from_the_manifest(self):
        title = sections_lib.place_title(
            SUBJECT, "6_c1", None, {"6_c1": "Партизанское движение"}
        )
        assert "Партизанское движение" in title

    def test_long_title_is_cut_at_the_phrase(self):
        label = sections_lib.button_label(
            "Древнейшие верования. Искусство. Особенности древнейших цивилизаций"
        )
        assert label == "Древнейшие верования…"

    def test_short_title_is_left_alone(self):
        assert sections_lib.button_label("Образование ВКЛ") == "Образование ВКЛ"

    def test_title_without_phrases_is_cut_by_length(self):
        label = sections_lib.button_label("а" * 80)
        assert len(label) <= sections_lib.MAX_BUTTON
        assert label.endswith("…")


class TestStudentSelection:
    """Что видит ученик, когда вопросы разложены по темам."""

    class _Bundle:
        def __init__(self, rows, teacher_id=None):
            self.rows = rows
            self.teacher_id = teacher_id
            self.subject = SUBJECT

    def _rows(self, *keys):
        return [{"Раздел": key, "Вопрос": f"в{i}"} for i, key in enumerate(keys)]

    def test_section_counts_include_its_topics(self):
        from bot.handlers.tests import _available_sections

        bundle = self._Bundle(self._rows("6_1", "6_8", "6"))
        available = dict(
            (key, title) for key, title in _available_sections(bundle)
        )
        assert "6" in available

    def test_section_full_of_topics_is_not_empty(self):
        """Раздел, где всё разложено по темам, обязан попасть в список."""
        from bot.handlers.tests import _available_sections

        bundle = self._Bundle(self._rows("6_1", "6_1"))
        assert [key for key, _ in _available_sections(bundle)] == ["6"]

    def test_only_non_empty_topics_are_offered(self):
        from bot.handlers.tests import _available_topics

        bundle = self._Bundle(self._rows("6_1", "6_1", "6_8"))
        offered = _available_topics(bundle, "6")

        assert [(key, count) for key, _, count in offered] == [("6_1", 2), ("6_8", 1)]

    def test_topics_of_another_section_are_not_offered(self):
        from bot.handlers.tests import _available_topics

        bundle = self._Bundle(self._rows("6_1", "5_2"))
        assert [key for key, _, _ in _available_topics(bundle, "5")] == ["5_2"]

    def test_section_without_topics_offers_none(self):
        from bot.handlers.tests import _available_topics

        bundle = self._Bundle(self._rows("6", "6"))
        assert _available_topics(bundle, "6") == []


class _Stub:
    """Заглушки сообщения и нажатия: собирают то, что бот отправил."""

    class Chat:
        def __init__(self, cid):
            self.id = cid

    class User:
        def __init__(self, uid):
            self.id = uid

    class Message:
        """Сообщение бота: можно заменить, если оно своё и с текстом.

        Сообщение пользователя редактировать нельзя — как и в Telegram,
        поэтому `edit_text` у него срывается, и мастер шлёт новое.
        """

        def __init__(self, text=None, uid=TEACHER, editable=True):
            self.text = text
            self.from_user = _Stub.User(uid)
            self.chat = _Stub.Chat(uid)
            self.editable = editable

        async def answer(self, text, reply_markup=None, **kw):
            _Stub.log.append(text)
            _Stub.sent.append(text)
            _Stub.keyboards.append(reply_markup)
            return self

        async def edit_text(self, text, reply_markup=None, **kw):
            if not self.editable:
                raise RuntimeError("message can't be edited")
            _Stub.log.append(text)
            _Stub.edited.append(text)
            _Stub.keyboards.append(reply_markup)
            return self

    class Callback:
        def __init__(self, data, uid=TEACHER):
            self.data = data
            self.from_user = _Stub.User(uid)
            self.message = _Stub.Message(uid=uid)
            self.bot = None

        async def answer(self, *a, **kw):
            return None

    log: list = []       # всё, что бот показал, в порядке появления
    sent: list = []      # новые сообщения
    edited: list = []    # замены уже показанного
    keyboards: list = []

    @classmethod
    def clear(cls) -> None:
        for bucket in (cls.log, cls.sent, cls.edited, cls.keyboards):
            bucket.clear()


class TestUploadWizard:
    """Мастер загрузки: раздел, затем тема, затем файл."""

    @pytest_asyncio.fixture
    async def flow(self, env):
        from aiogram.fsm.context import FSMContext
        from aiogram.fsm.storage.base import StorageKey
        from aiogram.fsm.storage.memory import MemoryStorage

        import bot.handlers.teacher_upload as tu
        import database.db as db_module
        from database.models import ROLE_TEACHER

        saved_db = db_module.DB_PATH
        db_module.DB_PATH = str(env / "wizard.sqlite3")
        try:
            await db_module.init_db()
            await db_module.ensure_user(TEACHER)
            await db_module.set_role(TEACHER, ROLE_TEACHER)
            await db_module.set_subject(TEACHER, SUBJECT)

            _upload(SAMPLE_ONE)
            teacher_content.rebuild(TEACHER, SUBJECT)

            _Stub.clear()
            yield tu, FSMContext(
                storage=MemoryStorage(), key=StorageKey(1, TEACHER, TEACHER)
            )
        finally:
            db_module.DB_PATH = saved_db

    async def test_section_leads_to_topics(self, flow):
        tu, state = flow

        await tu.sort_entry(_Stub.Callback("trainer:sort"), state)
        await tu.section_chosen(_Stub.Callback(f"{tu.SEC_PREFIX}:4"), state)

        assert any("Теперь тема" in text for text in _Stub.log)
        assert (await state.get_data()).get("sec_section") == "4"

    async def test_chosen_topic_lands_in_rows(self, flow):
        tu, state = flow

        await tu.sort_entry(_Stub.Callback("trainer:sort"), state)
        await tu.section_chosen(_Stub.Callback(f"{tu.SEC_PREFIX}:4"), state)
        await tu.topic_chosen(_Stub.Callback(f"{tu.TOP_PREFIX}:4_6"), state)

        rows = teacher_content.load_tests(TEACHER, SUBJECT)
        assert {row["Раздел"] for row in rows} == {"4_6"}

    async def test_whole_section_keeps_the_section_key(self, flow):
        tu, state = flow

        await tu.sort_entry(_Stub.Callback("trainer:sort"), state)
        await tu.section_chosen(_Stub.Callback(f"{tu.SEC_PREFIX}:4"), state)
        await tu.topic_chosen(
            _Stub.Callback(f"{tu.TOP_PREFIX}:{tu.WHOLE_SECTION_KEY}"), state
        )

        rows = teacher_content.load_tests(TEACHER, SUBJECT)
        assert {row["Раздел"] for row in rows} == {"4"}

    async def test_mixed_pile_skips_the_topic_step(self, flow):
        """У «Смешанных вопросов» тем нет — спрашивать нечего."""
        tu, state = flow

        await tu.sort_entry(_Stub.Callback("trainer:sort"), state)
        await tu.section_chosen(
            _Stub.Callback(f"{tu.SEC_PREFIX}:{tu.UNSORTED_KEY}"), state
        )

        assert not any("Теперь тема" in text for text in _Stub.log)

    async def test_back_returns_to_sections(self, flow):
        tu, state = flow

        await tu.sort_entry(_Stub.Callback("trainer:sort"), state)
        await tu.section_chosen(_Stub.Callback(f"{tu.SEC_PREFIX}:4"), state)
        _Stub.clear()
        await tu.topic_chosen(_Stub.Callback(f"{tu.TOP_PREFIX}:back"), state)

        assert any("раздел" in text.lower() for text in _Stub.log)
        # Файл при этом не разложен: назад — это не выбор
        rows = teacher_content.load_tests(TEACHER, SUBJECT)
        assert {row["Раздел"] for row in rows} == {""}

    async def test_own_topic_is_created_and_applied(self, flow):
        tu, state = flow

        await tu.sort_entry(_Stub.Callback("trainer:sort"), state)
        await tu.section_chosen(_Stub.Callback(f"{tu.SEC_PREFIX}:4"), state)
        await tu.topic_chosen(_Stub.Callback(f"{tu.TOP_PREFIX}:new"), state)
        await tu.topic_title_entered(_Stub.Message("Своя тема"), state)

        rows = teacher_content.load_tests(TEACHER, SUBJECT)
        placed = {row["Раздел"] for row in rows}
        assert len(placed) == 1
        key = placed.pop()
        assert sections_lib.section_of(key) == "4"
        assert teacher_content.custom_topics(TEACHER, SUBJECT)[key] == "Своя тема"

    async def test_invented_topic_is_refused(self, flow):
        tu, state = flow

        await tu.sort_entry(_Stub.Callback("trainer:sort"), state)
        await tu.section_chosen(_Stub.Callback(f"{tu.SEC_PREFIX}:4"), state)
        _Stub.clear()
        await tu.topic_chosen(_Stub.Callback(f"{tu.TOP_PREFIX}:4_99"), state)

        assert any("Такой темы нет" in text for text in _Stub.log)
        rows = teacher_content.load_tests(TEACHER, SUBJECT)
        assert {row["Раздел"] for row in rows} == {""}

    async def test_wizard_waits_for_the_file_after_topic(self, flow):
        """Мастер «раздел → тема → файл»: место запоминается, ждём документ."""
        tu, state = flow

        await tu._start_upload(_Stub.Message(), state, SUBJECT)
        await tu.section_chosen(_Stub.Callback(f"{tu.SEC_PREFIX}:4"), state)
        await tu.topic_chosen(_Stub.Callback(f"{tu.TOP_PREFIX}:4_6"), state)

        data = await state.get_data()
        assert data.get("section") == "4_6"
        assert await state.get_state() == tu.TeacherUpload.waiting_file

    async def test_sorting_old_files_does_not_hijack_the_next_upload(self, flow):
        """Раскладка старых загрузок — не выбор места для следующего файла.

        Иначе преподаватель, разобрав завалы, отправил бы новый файл
        в последнюю разобранную тему, ничего об этом не зная.
        """
        tu, state = flow

        await tu.sort_entry(_Stub.Callback("trainer:sort"), state)
        await tu.section_chosen(_Stub.Callback(f"{tu.SEC_PREFIX}:4"), state)
        await tu.topic_chosen(_Stub.Callback(f"{tu.TOP_PREFIX}:4_6"), state)

        assert (await state.get_data()).get("section") is None


class TestOneScreen:
    """Мастер живёт одним экраном, а не лентой сообщений.

    Каждый шаг отдельным сообщением — это чат, в котором до нужного места
    надо листать, а старые клавиатуры остаются живыми и по ним нажимают.
    """

    @pytest_asyncio.fixture
    async def flow(self, env):
        from aiogram.fsm.context import FSMContext
        from aiogram.fsm.storage.base import StorageKey
        from aiogram.fsm.storage.memory import MemoryStorage

        import bot.handlers.teacher_upload as tu
        import database.db as db_module
        from database.models import ROLE_TEACHER

        saved_db = db_module.DB_PATH
        db_module.DB_PATH = str(env / "screen.sqlite3")
        try:
            await db_module.init_db()
            await db_module.ensure_user(TEACHER)
            await db_module.set_role(TEACHER, ROLE_TEACHER)
            await db_module.set_subject(TEACHER, SUBJECT)

            _upload(SAMPLE_ONE)
            _upload(SAMPLE_TWO)
            teacher_content.rebuild(TEACHER, SUBJECT)

            _Stub.clear()
            yield tu, FSMContext(
                storage=MemoryStorage(), key=StorageKey(1, TEACHER, TEACHER)
            )
        finally:
            db_module.DB_PATH = saved_db

    async def test_steps_replace_each_other(self, flow):
        tu, state = flow

        await tu.sort_entry(_Stub.Callback("trainer:sort"), state)
        _Stub.clear()

        await tu.section_chosen(_Stub.Callback(f"{tu.SEC_PREFIX}:4"), state)
        await tu.topic_chosen(_Stub.Callback(f"{tu.TOP_PREFIX}:4_6"), state)

        assert _Stub.sent == []
        assert len(_Stub.edited) == 2

    async def test_sorting_a_pile_does_not_pile_up_messages(self, flow):
        """Два файла — два экрана, а не четыре сообщения."""
        tu, state = flow

        await tu.sort_entry(_Stub.Callback("trainer:sort"), state)
        _Stub.clear()

        for _ in range(2):
            await tu.section_chosen(_Stub.Callback(f"{tu.SEC_PREFIX}:4"), state)
            await tu.topic_chosen(
                _Stub.Callback(f"{tu.TOP_PREFIX}:{tu.WHOLE_SECTION_KEY}"), state
            )

        assert _Stub.sent == []

    async def test_result_of_the_previous_file_is_not_lost(self, flow):
        """Экран один, но судьба разложенного файла на нём видна."""
        tu, state = flow

        await tu.sort_entry(_Stub.Callback("trainer:sort"), state)
        await tu.section_chosen(_Stub.Callback(f"{tu.SEC_PREFIX}:4"), state)
        _Stub.clear()
        await tu.topic_chosen(_Stub.Callback(f"{tu.TOP_PREFIX}:4_6"), state)

        assert any("→" in text for text in _Stub.log)

    async def test_reply_to_a_typed_name_is_a_new_message(self, flow):
        """Сообщение пользователя заменить нельзя — отвечаем новым."""
        tu, state = flow

        await tu.sort_entry(_Stub.Callback("trainer:sort"), state)
        await tu.section_chosen(_Stub.Callback(f"{tu.SEC_PREFIX}:4"), state)
        await tu.topic_chosen(_Stub.Callback(f"{tu.TOP_PREFIX}:new"), state)
        _Stub.clear()

        await tu.topic_title_entered(
            _Stub.Message("Своя тема", editable=False), state
        )

        assert len(_Stub.sent) == 1
        assert _Stub.edited == []


class TestBackFromQuantity:
    """Кнопка «Назад» на экране количества.

    Раньше там было «Меню»: ошибившись разделом, ученик улетал в самое начало
    и заходил заново. Назад должно вести на шаг назад — а какой это шаг,
    зависит от того, как сюда пришли.
    """

    class _Bundle:
        def __init__(self, rows):
            self.rows = rows
            self.teacher_id = None
            self.subject = SUBJECT
            self.empty_reason = ""
            self.fallback = False

        def __bool__(self):
            return bool(self.rows)

    @pytest.fixture
    def screen(self, monkeypatch):
        """Хендлеры тестов с подменённым набором вопросов ученика."""
        from aiogram.fsm.context import FSMContext
        from aiogram.fsm.storage.base import StorageKey
        from aiogram.fsm.storage.memory import MemoryStorage

        import bot.handlers.tests as th

        rows = [
            {"Раздел": "6_1", "Вопрос": "1", "Вариант": "в1"},
            {"Раздел": "6_8", "Вопрос": "2", "Вариант": "в1"},
            {"Раздел": "5", "Вопрос": "3", "Вариант": "в2"},
        ]
        bundle = self._Bundle(rows)

        async def fake_get_tests(_user_id):
            return bundle

        monkeypatch.setattr(th.content_provider, "get_tests", fake_get_tests)
        _Stub.clear()
        return th, FSMContext(
            storage=MemoryStorage(), key=StorageKey(1, STUDENT, STUDENT)
        )

    async def test_from_topic_back_to_topics(self, screen):
        th, state = screen

        await th.pick_section(_Stub.Callback("tests:section:6_1", uid=STUDENT), state)
        _Stub.clear()
        await th.qty_back(_Stub.Callback("tests:qty_back", uid=STUDENT), state)

        assert any("Выбери тему" in text for text in _Stub.log)

    async def test_from_whole_section_back_to_topics(self, screen):
        """«Весь раздел целиком» тоже пришёл со списка тем."""
        th, state = screen

        await th.pick_section_whole(_Stub.Callback("tests:secall:6", uid=STUDENT), state)
        _Stub.clear()
        await th.qty_back(_Stub.Callback("tests:qty_back", uid=STUDENT), state)

        assert any("Выбери тему" in text for text in _Stub.log)

    async def test_from_section_without_topics_back_to_sections(self, screen):
        th, state = screen

        await th.pick_section(_Stub.Callback("tests:section:5", uid=STUDENT), state)
        _Stub.clear()
        await th.qty_back(_Stub.Callback("tests:qty_back", uid=STUDENT), state)

        assert any("Выбери раздел" in text for text in _Stub.log)

    async def test_from_part_b_back_to_the_root(self, screen):
        th, state = screen

        await th.by_part_b(_Stub.Callback("tests:part_b", uid=STUDENT), state)
        _Stub.clear()
        await th.qty_back(_Stub.Callback("tests:qty_back", uid=STUDENT), state)

        assert any("Тесты" in text for text in _Stub.log)

    async def test_back_never_lands_in_the_main_menu(self, screen):
        """Главное меню — это не «назад», а выход из режима."""
        th, state = screen

        for data in ("tests:section:6_1", "tests:section:5"):
            await th.pick_section(_Stub.Callback(data, uid=STUDENT), state)
            _Stub.clear()
            await th.qty_back(_Stub.Callback("tests:qty_back", uid=STUDENT), state)
            assert not any("Главное меню" in text for text in _Stub.log)

    def test_quantity_screen_offers_back_not_menu(self):
        from bot.keyboards.inline import quantity_kb

        last = quantity_kb().inline_keyboard[-1][0]
        assert last.callback_data == "tests:qty_back"
        assert "Назад" in last.text


class TestSectionTicket:
    """Целый билет по одному разделу.

    То же, что «полный билет» в главном меню, но вопросы берутся из раздела:
    после разбора темы на занятии осмысленно прогнать билет по этому разделу,
    а не по всему курсу.
    """

    class _Bundle:
        def __init__(self, rows):
            self.rows = rows
            self.teacher_id = None
            self.subject = SUBJECT
            self.empty_reason = ""
            self.fallback = False

        def __bool__(self):
            return bool(self.rows)

    def _rows(self, section, count, with_options=True):
        out = []
        for i in range(count):
            row = {
                "Раздел": section, "Вариант": "", "Часть": "А" if with_options else "В",
                "№": str(i), "Вопрос": f"Вопрос {section} {i}?", "Ответ": "2",
            }
            for n in range(1, 6):
                row[f"Вар.{n}"] = f"вариант {n}" if with_options and n <= 4 else ""
            out.append(row)
        return out

    @pytest.fixture
    def screen(self, monkeypatch):
        from aiogram.fsm.context import FSMContext
        from aiogram.fsm.storage.base import StorageKey
        from aiogram.fsm.storage.memory import MemoryStorage

        import bot.handlers.tests as th

        rows = (
            self._rows("6_1", 30) + self._rows("6_8", 30, with_options=False)
            + self._rows("5", 40) + self._rows("5", 40, with_options=False)
        )
        bundle = self._Bundle(rows)
        captured = {}

        async def fake_get_tests(_user_id):
            return bundle

        async def fake_send_question(*a, **kw):
            return None

        monkeypatch.setattr(th.content_provider, "get_tests", fake_get_tests)
        monkeypatch.setattr(th, "_send_question", fake_send_question)
        _Stub.clear()
        return th, FSMContext(
            storage=MemoryStorage(), key=StorageKey(1, STUDENT, STUDENT)
        ), captured

    async def test_whole_section_screen_offers_a_ticket(self, screen):
        th, state, _ = screen

        await th.pick_section_whole(_Stub.Callback("tests:secall:6", uid=STUDENT), state)

        buttons = [b for row in _Stub.keyboards[-1].inline_keyboard for b in row]
        ticket = [b for b in buttons if b.callback_data == "tests:secticket:6"]
        assert ticket, "кнопки билета по разделу нет"
        assert "38" in ticket[0].text
        # Выбор количества никуда не делся
        assert any(b.callback_data == "tests:qty:10" for b in buttons)

    async def test_ticket_is_built_from_that_section_only(self, screen):
        """Вопросы соседнего раздела в билет попасть не должны."""
        th, state, _ = screen

        await th.section_ticket(_Stub.Callback("tests:secticket:6", uid=STUDENT), state)
        session = (await state.get_data()).get("test_session") or {}
        assert session.get("questions")
        for q in session["questions"]:
            assert " 6_" in q["question_text"], q["question_text"]

    async def test_section_takes_in_its_topics(self, screen):
        """Раздел 6 разложен по темам — билет должен собраться из них."""
        th, state, _ = screen

        await th.section_ticket(_Stub.Callback("tests:secticket:6", uid=STUDENT), state)
        session = (await state.get_data()).get("test_session") or {}

        kinds = {q["type"] for q in session["questions"]}
        assert kinds == {"A", "B"}

    async def test_ticket_keeps_the_ticket_shape(self, screen):
        """16 вопросов части А и 22 части Б — как в полном билете."""
        th, state, _ = screen

        await th.section_ticket(_Stub.Callback("tests:secticket:5", uid=STUDENT), state)
        session = (await state.get_data()).get("test_session") or {}
        questions = session["questions"]

        assert len([q for q in questions if q["type"] == "A"]) == th.TICKET_PART_A
        assert len([q for q in questions if q["type"] == "B"]) == th.TICKET_PART_B

    async def test_short_section_says_how_many_there_are(self, screen):
        """Обещать 38 и молча выдать 16 нельзя.

        В теме 6_1 только вопросы части А, части Б там нет вовсе — билет
        выйдет короче, и на экране должно стоять настоящее число.
        """
        th, state, _ = screen

        await th.section_ticket(_Stub.Callback("tests:secticket:6_1", uid=STUDENT), state)
        session = (await state.get_data()).get("test_session") or {}

        assert len(session["questions"]) == th.TICKET_PART_A
        assert any(f"всего: {th.TICKET_PART_A}" in text for text in _Stub.log)

    async def test_section_name_is_shown(self, screen):
        th, state, _ = screen

        await th.section_ticket(_Stub.Callback("tests:secticket:6", uid=STUDENT), state)

        assert any("Билет:" in text and "1917" in text for text in _Stub.log)


class TestBaseMerge:
    """Общие вопросы из двух листов складываются в один набор.

    Обычные вопросы размечены разделами, отдельный лист «Вопросы по темам» —
    темами. Второй ученику не показывался вовсе. Склейка кладёт тему в то же
    поле «Раздел», поэтому темы у общих вопросов появляются без отдельной
    ветки в коде.
    """

    def _topic_row(self, **over):
        row = {
            "Раздел_№": "6", "Тема_код": "6_1", "Тема_название": "Тема",
            "Часть": "А", "№": "1", "Вопрос": "Кто?",
            "Вар1": "а", "Вар2": "б", "Вар3": "", "Вар4": "", "Вар5": "",
            "Ответ": "2",
        }
        row.update(over)
        return row

    def test_columns_are_renamed_to_the_row_schema(self):
        from services.sheets import topic_rows_as_tests

        row = topic_rows_as_tests([self._topic_row()])[0]

        assert row["Раздел"] == "6_1"
        assert row["Вар.1"] == "а"
        assert row["Вар.2"] == "б"
        assert row["Вопрос"] == "Кто?"
        assert row["Ответ"] == "2"

    def test_schema_matches_the_ordinary_rows(self):
        """Схема строки — общая для всего бота, лишних и недостающих полей быть не должно."""
        from services.sheets import topic_rows_as_tests

        row = topic_rows_as_tests([self._topic_row()])[0]
        expected = {
            "Вариант", "Часть", "№", "Вопрос",
            "Вар.1", "Вар.2", "Вар.3", "Вар.4", "Вар.5", "Ответ", "Раздел",
        }
        assert set(row) == expected

    def test_questions_without_a_topic_are_dropped(self):
        from services.sheets import topic_rows_as_tests

        assert topic_rows_as_tests([self._topic_row(Тема_код="")]) == []

    def test_questions_without_an_answer_are_dropped(self):
        """Тот же фильтр качества, что у билетов: без ответа проверять нечем."""
        from services.sheets import topic_rows_as_tests

        assert topic_rows_as_tests([self._topic_row(Ответ="  ")]) == []
        assert topic_rows_as_tests([self._topic_row(Вопрос="")]) == []

    def test_duplicates_of_the_ordinary_set_are_dropped(self):
        from services.sheets import topic_rows_as_tests

        known = [{"Вопрос": "  кто?  "}]
        assert topic_rows_as_tests([self._topic_row()], known) == []

    def test_repeats_inside_the_topic_set_are_dropped(self):
        from services.sheets import topic_rows_as_tests

        pair = [self._topic_row(), self._topic_row(Тема_код="6_2")]
        assert len(topic_rows_as_tests(pair)) == 1

    def test_part_letter_typo_is_normalised(self):
        """«Б» вместо «В» — опечатка разметки, часть в билете одна и та же."""
        from services.sheets import topic_rows_as_tests

        assert topic_rows_as_tests([self._topic_row(Часть="Б")])[0]["Часть"] == "В"

    def test_topic_questions_have_no_ticket_variant(self):
        """Эти вопросы не из билета — в тренировке по варианту им делать нечего."""
        from services.sheets import topic_rows_as_tests

        assert topic_rows_as_tests([self._topic_row()])[0]["Вариант"] == ""

    def test_combined_set_is_recomputed_when_rows_change(self):
        """Иначе подменённый набор молча отдавал бы прежние вопросы."""
        from services.sheets import SheetsCache

        cache = SheetsCache(
            tests_rows=[], topic_tests_rows=[self._topic_row()],
            flash_dates=[], flash_concepts=[], flash_persons=[],
        )
        assert len(cache.base_tests_rows) == 1

        cache.topic_tests_rows = [self._topic_row(), self._topic_row(Вопрос="Где?")]
        assert len(cache.base_tests_rows) == 2

    def test_real_snapshot_gains_topics(self):
        """На настоящем снимке: темы появляются, вопросы не теряются."""
        from services.sheets import sheets_cache

        if not sheets_cache.load_from_disk():
            pytest.skip("нет снимка общего контента")

        rows = sheets_cache.base_tests_rows
        with_topics = [r for r in rows if sections_lib.is_topic(str(r.get("Раздел")))]

        assert len(rows) > len(sheets_cache.tests_rows)
        assert with_topics
        # Каждая тема из данных известна сетке — иначе вопросы безымянные
        for row in with_topics:
            key = str(row["Раздел"])
            assert sections_lib.is_valid(SUBJECT, key), key


class TestMatching:
    """Отбор вопросов по выбранному месту."""

    def test_section_takes_in_its_topics(self):
        from bot.handlers.tests import _match_section

        assert _match_section("6_1", "6")
        assert _match_section("6_9_10", "6")

    def test_topic_takes_only_itself(self):
        from bot.handlers.tests import _match_section

        assert _match_section("6_1", "6_1")
        assert not _match_section("6", "6_1")
        # «6_1» и «6_10» — разные темы, и вхождение строки их путает
        assert not _match_section("6_10", "6_1")

    def test_neighbours_do_not_leak(self):
        from bot.handlers.tests import _match_section

        assert not _match_section("5_1", "6")
        assert not _match_section("6_1", "5")

    def test_mixed_pile_stays_separate(self):
        from bot.handlers.tests import _match_section

        assert not _match_section("6_1", "")
        assert _match_section("", "")

    def test_custom_section_takes_in_its_own_topics(self):
        from bot.handlers.tests import _match_section

        assert _match_section("c1_c1", "c1")
        assert not _match_section("c1_c1", "c2")
        assert not _match_section("c1_c1", "1")
