"""Жалобы на общие вопросы базы — решение у админа и сразу у всех.

Решено 13.09.2026. Раньше жалоба на общий вопрос скрывала его только
в группе одного преподавателя, и брак чинился кусочками: кривой вопрос,
замеченный у одних, продолжал попадаться всем остальным. А карточку
получал преподаватель, который этот вопрос не писал.

Что здесь проверяется в первую очередь:

- **двое друзей из одной группы базу не вычистят** — нужны жалобы учеников
  двух разных преподавателей;
- **скрытый общий вопрос пропадает у всех**, в том числе у тех, кто
  на него не жаловался;
- **возвращённый админом вопрос по жалобам больше не скрывается**;
- **свои вопросы преподавателя работают по-старому.**
"""

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
from services import content_provider, reports, storage  # noqa: E402
from services.sheets import sheets_cache  # noqa: E402

SUBJECT = "history"
OLGA, IRINA, THIRD = 9501, 9502, 9503
OLGA_KID_1, OLGA_KID_2 = 9601, 9602
IRINA_KID = 9611
THIRD_KID = 9621

BASE_Q = "Определите три правильных утверждения о периодизации древнейшей истории"
BASE_OK = "Какое государство стало крупнейшей колониальной империей?"
OWN_Q = "Кто основал Полоцкое княжество по версии летописи?"


def _row(question):
    return {
        "Вариант": "", "Часть": "А", "№": "1", "Вопрос": question,
        "Вар.1": "раз", "Вар.2": "два", "Вар.3": "", "Вар.4": "", "Вар.5": "",
        "Ответ": "1", "Раздел": "1",
    }


@pytest_asyncio.fixture
async def env():
    tmpdir = Path(tempfile.mkdtemp(prefix="aida_base_reports_"))
    saved = (
        db_module.DB_PATH,
        storage.DATA_ROOT, storage.SUBJECTS_ROOT, storage.TEACHERS_ROOT,
        sheets_cache.tests_rows, sheets_cache.topic_tests_rows,
    )
    db_module.DB_PATH = str(tmpdir / "test.sqlite3")
    storage.DATA_ROOT = tmpdir / "data"
    storage.SUBJECTS_ROOT = storage.DATA_ROOT / "subjects"
    storage.TEACHERS_ROOT = storage.DATA_ROOT / "teachers"
    sheets_cache.tests_rows = [_row(BASE_Q), _row(BASE_OK)]
    sheets_cache.topic_tests_rows = []
    try:
        await db_module.init_db()
        for teacher, kids in ((OLGA, (OLGA_KID_1, OLGA_KID_2)),
                              (IRINA, (IRINA_KID,)), (THIRD, (THIRD_KID,))):
            await db_module.ensure_user(teacher)
            await db_module.set_role(teacher, ROLE_TEACHER)
            await db_module.set_subject(teacher, SUBJECT)
            await db_module.ensure_teacher(teacher, SUBJECT)
            storage.ensure_teacher_dirs(teacher, SUBJECT)
            _write_own_tests(teacher, [])
            for kid in kids:
                await db_module.ensure_user(kid)
                await db_module.bind_student(kid, teacher, SUBJECT)
        yield tmpdir
    finally:
        (
            db_module.DB_PATH,
            storage.DATA_ROOT, storage.SUBJECTS_ROOT, storage.TEACHERS_ROOT,
            sheets_cache.tests_rows, sheets_cache.topic_tests_rows,
        ) = saved
        shutil.rmtree(tmpdir, ignore_errors=True)


def _write_own_tests(teacher, questions):
    storage.write_json(
        storage.teacher_tests_path(teacher, SUBJECT),
        {"parser_version": 4, "subject": SUBJECT, "rows": [_row(q) for q in questions]},
    )


async def _complain(kid, teacher, question, reason=reports.REASON_INCORRECT):
    """Ровно то, что делает хендлер жалобы для общего вопроса."""
    qhash = db_module.question_hash(question)
    await db_module.add_question_report(teacher, SUBJECT, qhash, kid, reason, question)
    if content_provider.is_base_question(qhash, teacher, SUBJECT):
        status = await db_module.get_base_question_status(qhash)
        teachers = await db_module.count_report_teachers(
            qhash, list(reports.BROKEN_REASONS)
        )
        if reports.should_hide_base(teachers, status):
            await db_module.set_base_question_status(qhash, reports.STATUS_HIDDEN)
    return qhash


async def _visible_to(kid, rows):
    user = await db_module.get_user(kid)
    kept = await content_provider._drop_reported(rows, user, user.teacher_id)
    return [r["Вопрос"] for r in kept]


class TestThreshold:
    def test_one_group_is_not_enough(self):
        assert reports.should_hide_base(1) is False

    def test_two_groups_hide(self):
        assert reports.should_hide_base(2) is True

    def test_returned_question_stays(self):
        assert reports.should_hide_base(5, reports.STATUS_CONFIRMED) is False


class TestGlobalHiding:
    async def test_two_friends_from_one_group_do_not_clean_the_base(self, env):
        await _complain(OLGA_KID_1, OLGA, BASE_Q)
        qhash = await _complain(OLGA_KID_2, OLGA, BASE_Q)

        assert await db_module.get_base_question_status(qhash) is None
        # У ученика другого преподавателя вопрос на месте
        assert BASE_Q in await _visible_to(THIRD_KID, sheets_cache.base_tests_rows)

    async def test_two_groups_hide_it_for_everyone(self, env):
        await _complain(OLGA_KID_1, OLGA, BASE_Q)
        qhash = await _complain(IRINA_KID, IRINA, BASE_Q)

        assert await db_module.get_base_question_status(qhash) == reports.STATUS_HIDDEN
        # Ученик третьего преподавателя не жаловался — но и ему не показываем
        visible = await _visible_to(THIRD_KID, sheets_cache.base_tests_rows)
        assert BASE_Q not in visible
        assert BASE_OK in visible

    async def test_unclear_does_not_count(self, env):
        """«Не понял» — не брак, сколько бы групп ни споткнулось."""
        await _complain(OLGA_KID_1, OLGA, BASE_Q, reports.REASON_UNCLEAR)
        qhash = await _complain(IRINA_KID, IRINA, BASE_Q, reports.REASON_UNCLEAR)

        assert await db_module.get_base_question_status(qhash) is None

    async def test_returned_question_is_not_hidden_again(self, env):
        await _complain(OLGA_KID_1, OLGA, BASE_Q)
        qhash = await _complain(IRINA_KID, IRINA, BASE_Q)
        await db_module.set_base_question_status(qhash, reports.STATUS_CONFIRMED)

        await _complain(THIRD_KID, THIRD, BASE_Q)

        assert await db_module.get_base_question_status(qhash) == reports.STATUS_CONFIRMED
        assert BASE_Q in await _visible_to(OLGA_KID_2, sheets_cache.base_tests_rows)

    async def test_complainer_stops_seeing_it_right_away(self, env):
        """Порог не набран, но тот, кто нашёл брак, его больше не видит."""
        await _complain(OLGA_KID_1, OLGA, BASE_Q)

        assert BASE_Q not in await _visible_to(OLGA_KID_1, sheets_cache.base_tests_rows)
        assert BASE_Q in await _visible_to(OLGA_KID_2, sheets_cache.base_tests_rows)


class TestTeacherNoLongerRulesBase:
    async def test_teacher_flag_on_a_base_question_is_ignored(self, env):
        """Старый флаг преподавателя на общий вопрос больше не действует:
        общими распоряжается только админ."""
        qhash = db_module.question_hash(BASE_Q)
        await db_module.set_question_status(OLGA, SUBJECT, qhash, reports.STATUS_HIDDEN)

        assert BASE_Q in await _visible_to(OLGA_KID_1, sheets_cache.base_tests_rows)

    async def test_base_complaints_leave_the_teacher_screen(self, env):
        await _complain(OLGA_KID_1, OLGA, BASE_Q)
        items = await db_module.get_question_reports(OLGA, SUBJECT)

        assert content_provider.own_reports(items, OLGA, SUBJECT) == []

    async def test_own_questions_still_go_to_the_teacher(self, env):
        _write_own_tests(OLGA, [OWN_Q])
        await _complain(OLGA_KID_1, OLGA, OWN_Q)
        items = await db_module.get_question_reports(OLGA, SUBJECT)

        own = content_provider.own_reports(items, OLGA, SUBJECT)
        assert [i["question_hash"] for i in own] == [db_module.question_hash(OWN_Q)]

    async def test_identical_text_uploaded_by_teacher_is_his(self, env):
        """Вопрос с текстом как в базе, но из файла преподавателя, — его."""
        _write_own_tests(OLGA, [BASE_Q])
        qhash = db_module.question_hash(BASE_Q)

        assert content_provider.is_base_question(qhash, OLGA, SUBJECT) is False
        assert content_provider.is_base_question(qhash, IRINA, SUBJECT) is True


class TestAdminCard:
    async def test_card_shows_question_and_groups(self, env):
        from bot.handlers.base_reports import card_text

        await _complain(OLGA_KID_1, OLGA, BASE_Q)
        qhash = await _complain(IRINA_KID, IRINA, BASE_Q, reports.REASON_NO_OPTIONS)

        text = await card_text(qhash, "⚠️ Общий вопрос скрыт у всех")

        assert BASE_Q in text
        assert "1) раз" in text
        assert "Ответ в базе: 1" in text
        assert "от учеников 2 преподавателей" in text
        assert "составлен некорректно — 1" in text
        assert "нет вариантов ответа — 1" in text

    async def test_queue_holds_only_undecided(self, env):
        from bot.handlers.base_reports import _screen

        await _complain(OLGA_KID_1, OLGA, BASE_Q)
        qhash = await _complain(IRINA_KID, IRINA, BASE_Q)

        text, markup = await _screen(0)
        assert BASE_Q in text and markup is not None

        await db_module.set_base_question_status(qhash, reports.STATUS_REMOVED)
        text, markup = await _screen(0)
        assert "всё разобрано" in text
        assert markup is None
