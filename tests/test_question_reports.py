"""Жалобы учеников на вопросы.

Главное, что здесь проверяется: жалоба «не понял» не должна вести себя
как жалоба на брак. Если бы вела — из тренажёра исчезали бы ровно самые
сложные вопросы, а на экзамене формулировка будет та же.
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
from services import content_provider, reports, storage, teacher_content  # noqa: E402

SUBJECT = "history"
TEACHER = 7701
ANYA, MASHA, PETR = 7801, 7802, 7803

QUESTION = "В рамках торгового треугольника из Африки в Америку везли:"
OTHER = "Какое государство стало крупнейшей колониальной империей?"


@pytest_asyncio.fixture
async def env():
    tmpdir = Path(tempfile.mkdtemp(prefix="aida_reports_"))
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

        for sid, name in ((ANYA, "Аня"), (MASHA, "Маша"), (PETR, "Пётр")):
            await db_module.ensure_user(sid)
            await db_module.set_profile(sid, name=name)
            await db_module.bind_student(sid, TEACHER, SUBJECT)

        _write_tests([QUESTION, OTHER])
        yield tmpdir
    finally:
        (
            db_module.DB_PATH,
            storage.DATA_ROOT, storage.SUBJECTS_ROOT, storage.TEACHERS_ROOT,
        ) = saved
        shutil.rmtree(tmpdir, ignore_errors=True)


def _write_tests(questions):
    rows = [
        {
            "Вариант": "тест", "Часть": "А", "№": str(i), "Вопрос": q,
            "Вар.1": "раз", "Вар.2": "два", "Вар.3": "", "Вар.4": "", "Вар.5": "",
            "Ответ": "1", "Раздел": "4",
        }
        for i, q in enumerate(questions, 1)
    ]
    storage.write_json(
        storage.teacher_tests_path(TEACHER, SUBJECT),
        {"parser_version": 1, "subject": SUBJECT, "updated_at": "", "rows": rows},
    )


async def _report(student: int, question: str, reason: str):
    return await db_module.add_question_report(
        TEACHER, SUBJECT, db_module.question_hash(question), student, reason, question
    )


async def _visible(student: int) -> int:
    bundle = await content_provider.get_tests(student)
    return len(bundle.rows)


class TestRules:
    """Правила без базы: что считается браком и когда прятать."""

    def test_broken_reasons(self):
        assert reports.is_broken(reports.REASON_NO_OPTIONS)
        assert reports.is_broken(reports.REASON_INCORRECT)
        assert not reports.is_broken(reports.REASON_UNCLEAR)

    def test_threshold_counts_only_broken(self):
        unclear = [reports.REASON_UNCLEAR] * 5
        assert reports.should_hide(unclear) is False

        mixed = [reports.REASON_UNCLEAR, reports.REASON_NO_OPTIONS]
        assert reports.should_hide(mixed) is False

        broken = [reports.REASON_NO_OPTIONS, reports.REASON_INCORRECT]
        assert reports.should_hide(broken) is True

    def test_confirmed_question_never_hides_again(self):
        broken = [reports.REASON_NO_OPTIONS, reports.REASON_INCORRECT]
        assert reports.should_hide(broken, reports.STATUS_CONFIRMED) is False

    def test_only_broken_hides_for_the_complainer(self):
        assert reports.hides_for_student(reports.REASON_NO_OPTIONS) is True
        assert reports.hides_for_student(reports.REASON_UNCLEAR) is False

    def test_unknown_reason_refused(self):
        assert reports.is_valid_reason("что-то ещё") is False


class TestVisibility:
    """Что после жалобы видит ученик."""

    async def test_broken_hides_for_the_complainer_only(self, env):
        assert await _visible(ANYA) == 2

        await _report(ANYA, QUESTION, reports.REASON_NO_OPTIONS)

        assert await _visible(ANYA) == 1
        assert await _visible(MASHA) == 2  # чужая жалоба её не касается

    async def test_unclear_keeps_the_question(self, env):
        await _report(PETR, QUESTION, reports.REASON_UNCLEAR)

        # Непонятый вопрос остаётся: его и надо прорешать после объяснения
        assert await _visible(PETR) == 2

    async def test_second_broken_report_hides_for_everyone(self, env):
        await _report(ANYA, QUESTION, reports.REASON_NO_OPTIONS)
        collected = await _report(MASHA, QUESTION, reports.REASON_INCORRECT)

        assert reports.should_hide(collected) is True
        await db_module.set_question_status(
            TEACHER, SUBJECT, db_module.question_hash(QUESTION), reports.STATUS_HIDDEN
        )

        assert await _visible(PETR) == 1  # не жаловался, но вопрос скрыт

    async def test_two_unclear_reports_hide_nothing(self, env):
        await _report(ANYA, QUESTION, reports.REASON_UNCLEAR)
        collected = await _report(MASHA, QUESTION, reports.REASON_UNCLEAR)

        assert reports.should_hide(collected) is False
        assert await _visible(PETR) == 2

    async def test_removed_question_disappears(self, env):
        await db_module.set_question_status(
            TEACHER, SUBJECT, db_module.question_hash(QUESTION), reports.STATUS_REMOVED
        )
        assert await _visible(ANYA) == 1

    async def test_confirmed_question_stays_visible(self, env):
        await db_module.set_question_status(
            TEACHER, SUBJECT, db_module.question_hash(QUESTION), reports.STATUS_CONFIRMED
        )
        assert await _visible(ANYA) == 2


class TestTeacherList:
    """Список у преподавателя."""

    async def test_report_appears_with_name_and_reason(self, env):
        await _report(ANYA, QUESTION, reports.REASON_NO_OPTIONS)

        items = await db_module.get_question_reports(TEACHER, SUBJECT)
        assert len(items) == 1
        assert items[0]["students"] == ["Аня"]
        assert items[0]["reasons"] == [reports.REASON_NO_OPTIONS]

    async def test_repeated_report_replaces_the_previous_one(self, env):
        await _report(ANYA, QUESTION, reports.REASON_UNCLEAR)
        collected = await _report(ANYA, QUESTION, reports.REASON_NO_OPTIONS)

        # Передумал — значит причина теперь другая, а не две сразу
        assert collected == [reports.REASON_NO_OPTIONS]

    async def test_decision_closes_the_report(self, env):
        await _report(ANYA, QUESTION, reports.REASON_NO_OPTIONS)
        await db_module.clear_question_reports(TEACHER, db_module.question_hash(QUESTION))

        assert await db_module.get_question_reports(TEACHER, SUBJECT) == []

    async def test_hidden_comes_first(self, env):
        from bot.handlers.menu import _sort_reports

        await _report(ANYA, OTHER, reports.REASON_UNCLEAR)
        await _report(MASHA, QUESTION, reports.REASON_NO_OPTIONS)
        await db_module.set_question_status(
            TEACHER, SUBJECT, db_module.question_hash(QUESTION), reports.STATUS_HIDDEN
        )

        items = _sort_reports(await db_module.get_question_reports(TEACHER, SUBJECT))
        assert items[0]["question_hash"] == db_module.question_hash(QUESTION)

    async def test_card_shows_the_question_as_the_student_sees_it(self, env):
        from bot.handlers.menu import _find_question, _report_card

        await _report(ANYA, QUESTION, reports.REASON_NO_OPTIONS)
        items = await db_module.get_question_reports(TEACHER, SUBJECT)
        row = _find_question(TEACHER, SUBJECT, items[0]["question_hash"])
        card = _report_card(items[0], row, 1, 1)

        assert QUESTION in card
        assert "1. раз" in card          # варианты, чтобы было чем судить
        assert "Правильный ответ: 1" in card
        assert "Аня" in card

    async def test_base_layer_question_is_found(self, env):
        from bot.handlers.menu import _find_question
        from services.sheets import sheets_cache

        # Ученики занимаются по общему набору, пока преподаватель ничего
        # не загрузил — жалоба приходит именно на такой вопрос
        base_question = "Вопрос из общего набора предмета"
        saved = sheets_cache.tests_rows
        sheets_cache.tests_rows = [{
            "Вопрос": base_question, "Ответ": "2",
            "Вар.1": "раз", "Вар.2": "два", "Вар.3": "", "Вар.4": "", "Вар.5": "",
        }]
        try:
            row = _find_question(
                TEACHER, SUBJECT, db_module.question_hash(base_question)
            )
            assert row is not None
            assert row["Вопрос"] == base_question
        finally:
            sheets_cache.tests_rows = saved

    async def test_card_survives_a_deleted_question(self, env):
        from bot.handlers.menu import _report_card

        await _report(ANYA, "Вопрос из удалённого файла", reports.REASON_INCORRECT)
        items = await db_module.get_question_reports(TEACHER, SUBJECT)
        card = _report_card(items[0], None, 1, 1)

        assert "больше нет в тренажёре" in card


class TestScoring:
    """Пропущенный по жалобе вопрос не должен попадать в статистику."""

    def test_skipped_question_is_not_counted_as_correct(self):
        from bot.handlers.tests import _calc_result

        questions = [{"skipped": True}, {}, {}]
        correct, total = _calc_result(questions, wrong=[])

        assert (correct, total) == (2, 2)

    def test_wrong_answer_on_a_broken_question_is_forgiven(self):
        from bot.handlers.tests import _calc_result

        # Ответил неверно, потом отметил вопрос сломанным: ошибка не его
        questions = [{"skipped": True}, {}, {}]
        correct, total = _calc_result(questions, wrong=[0])

        assert (correct, total) == (2, 2)

    def test_correct_never_goes_negative(self):
        from bot.handlers.tests import _calc_result

        questions = [{"skipped": True}, {"skipped": True}]
        correct, total = _calc_result(questions, wrong=[0, 1])

        assert (correct, total) == (0, 0)

    def test_all_skipped(self):
        from bot.handlers.tests import _calc_result, _result_screen

        correct, total = _calc_result([{"skipped": True}], wrong=[])
        assert (correct, total) == (0, 0)
        assert "спорны" in _result_screen(correct, total, [])


class TestReportButton:
    """Кнопка жалобы: за какой вопрос она отвечает."""

    def test_button_carries_the_question_fingerprint(self):
        from bot.keyboards.inline import REPORT_PREFIX, question_report_kb

        qhash = db_module.question_hash(QUESTION)
        kb = question_report_kb(qhash)
        data = kb.inline_keyboard[0][0].callback_data

        # Не номер в наборе: старые сообщения живут в чате, и по номеру
        # жалоба попадала бы в чужой вопрос следующего теста
        assert data == f"{REPORT_PREFIX}:{qhash}"
        assert len(data.encode()) <= 64  # лимит Telegram на callback_data

    def test_reasons_keep_the_same_fingerprint(self):
        from bot.keyboards.inline import REASON_PREFIX, report_reasons_kb

        qhash = db_module.question_hash(QUESTION)
        kb = report_reasons_kb(qhash, list(reports.REASON_LABELS.items()))
        data = [b.callback_data for row in kb.inline_keyboard for b in row]

        assert f"{REASON_PREFIX}:{qhash}:{reports.REASON_UNCLEAR}" in data
        assert all(len(d.encode()) <= 64 for d in data)

    def test_finds_the_question_in_the_session(self):
        from bot.handlers.tests import _find_in_session

        session = {"questions": [
            {"question_text": OTHER},
            {"question_text": QUESTION},
        ]}
        pos, question = _find_in_session(session, db_module.question_hash(QUESTION))

        assert pos == 1
        assert question["question_text"] == QUESTION

    def test_missing_question_is_not_an_error(self):
        from bot.handlers.tests import _find_in_session

        # Сессии может уже не быть — жалоба всё равно должна записаться
        pos, question = _find_in_session({}, db_module.question_hash(QUESTION))
        assert (pos, question) == (None, None)
