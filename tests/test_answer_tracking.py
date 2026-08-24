"""Учёт ответов: журнал и счётчики.

Счётчики нельзя досчитать задним числом — журнал к тому времени вычищен
по возрасту. Поэтому здесь проверяется, что они пишутся сразу и переживают
очистку журнала.
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

TEACHER_A, TEACHER_B = 9601, 9602
STUDENT_1, STUDENT_2 = 9701, 9702
SUBJECT = "history"

Q1 = "Какое событие произошло в 1569 году?"
Q2 = "Что такое фольварк?"


@pytest_asyncio.fixture
async def env():
    tmpdir = Path(tempfile.mkdtemp(prefix="aida_ans_"))
    saved = db_module.DB_PATH
    db_module.DB_PATH = str(tmpdir / "test.sqlite3")
    try:
        await db_module.init_db()
        yield tmpdir
    finally:
        db_module.DB_PATH = saved
        shutil.rmtree(tmpdir, ignore_errors=True)


async def _answer(user_id, question, correct, teacher_id=TEACHER_A):
    await db_module.record_answer(
        user_id, question, is_correct=correct,
        teacher_id=teacher_id, subject=SUBJECT, student_answer="3",
    )


class TestCounters:
    async def test_repeated_answers_do_not_add_rows(self, env):
        """Десять ответов на один вопрос — по-прежнему одна строка счётчиков."""
        for _ in range(10):
            await _answer(STUDENT_1, Q1, correct=False)

        progress = await db_module.get_student_progress(STUDENT_1)
        assert progress["questions"] == 1
        assert progress["attempts"] == 10

    async def test_accuracy(self, env):
        await _answer(STUDENT_1, Q1, correct=True)
        await _answer(STUDENT_1, Q1, correct=False)
        await _answer(STUDENT_1, Q2, correct=True)

        progress = await db_module.get_student_progress(STUDENT_1)
        assert progress["questions"] == 2
        assert progress["attempts"] == 3
        assert progress["correct"] == 2
        assert progress["accuracy"] == 67

    async def test_mastered_counts_last_answer_only(self, env):
        """Ошибся, потом ответил верно — вопрос считается освоенным."""
        await _answer(STUDENT_1, Q1, correct=False)
        await _answer(STUDENT_1, Q1, correct=True)
        await _answer(STUDENT_1, Q2, correct=True)
        await _answer(STUDENT_1, Q2, correct=False)

        progress = await db_module.get_student_progress(STUDENT_1)
        assert progress["mastered"] == 1

    async def test_progress_is_per_student(self, env):
        await _answer(STUDENT_1, Q1, correct=True)
        assert (await db_module.get_student_progress(STUDENT_2))["attempts"] == 0

    async def test_empty_progress(self, env):
        progress = await db_module.get_student_progress(STUDENT_1)
        assert progress["attempts"] == 0
        assert progress["accuracy"] == 0
        assert progress["last_answered_at"] is None


class TestVerdict:
    """Что считать верным ответом.

    Раньше «не начинается с ❌» означало «верно», и в счётчики как верные
    попадали два разных случая: частичный зачёт и сбой проверки, когда
    модель недоступна. Первый завышал точность, второй ещё и откладывал
    вопрос на месяцы вперёд.
    """

    def test_correct(self):
        from services.llm import VERDICT_CORRECT, verdict_of
        assert verdict_of("✅ Верно!") == VERDICT_CORRECT

    def test_wrong(self):
        from services.llm import VERDICT_WRONG, verdict_of
        assert verdict_of("❌ Правильный ответ: 4") == VERDICT_WRONG

    def test_partial_is_not_correct(self):
        from services.llm import VERDICT_CORRECT, VERDICT_PARTIAL, verdict_of
        assert verdict_of("🟡 Частично верно! Ты выбрал 2 из 3") == VERDICT_PARTIAL
        assert verdict_of("🟡 Частично верно!") != VERDICT_CORRECT

    def test_failure_is_not_correct(self):
        """Модель недоступна — ответ не подтверждён и в статистику не идёт."""
        from services.llm import VERDICT_UNKNOWN, verdict_of
        assert verdict_of("Не удалось проверить ответ. Попробуй позже.") == VERDICT_UNKNOWN

    def test_empty_response(self):
        from services.llm import VERDICT_UNKNOWN, verdict_of
        assert verdict_of("") == VERDICT_UNKNOWN

    def test_leading_whitespace_is_tolerated(self):
        from services.llm import VERDICT_CORRECT, verdict_of
        assert verdict_of("\n  ✅ Верно!") == VERDICT_CORRECT


class TestScopedCounters:
    """Счётчик повторов и статистика класса не должны считать лишнее."""

    async def test_due_counts_only_current_teacher(self, env):
        await _answer(STUDENT_1, Q1, correct=False, teacher_id=TEACHER_A)
        await _answer(STUDENT_1, Q2, correct=False, teacher_id=TEACHER_B)

        assert await db_module.count_due(STUDENT_1) == 2
        assert await db_module.count_due(STUDENT_1, teacher_id=TEACHER_A) == 1

    async def test_due_counts_rows_without_schedule(self, env):
        """Строки, созданные до появления расписания, считаются просроченными."""
        await _answer(STUDENT_1, Q1, correct=True)
        import aiosqlite

        async with aiosqlite.connect(db_module.DB_PATH) as db:
            await db.execute("UPDATE answer_stats SET due_at = NULL")
            await db.commit()

        assert await db_module.count_due(STUDENT_1) == 1

    async def test_teacher_own_answers_are_not_class_stats(self, env):
        """Преподаватель проходит свой тренажёр — это не работа класса."""
        await _answer(TEACHER_A, Q1, correct=False, teacher_id=TEACHER_A)
        await _answer(STUDENT_1, Q1, correct=False, teacher_id=TEACHER_A)

        totals = await db_module.get_teacher_totals(TEACHER_A)
        assert totals["students"] == 1
        assert totals["attempts"] == 1

        hard = await db_module.get_hard_questions(TEACHER_A, min_errors=1)
        assert hard[0]["students"] == 1

    async def test_stats_are_split_by_subject(self, env):
        await db_module.record_answer(
            STUDENT_1, Q1, is_correct=False, teacher_id=TEACHER_A, subject="history"
        )
        await db_module.record_answer(
            STUDENT_1, Q2, is_correct=False, teacher_id=TEACHER_A, subject="russian"
        )

        assert (await db_module.get_teacher_totals(TEACHER_A))["attempts"] == 2
        history = await db_module.get_teacher_totals(TEACHER_A, subject="history")
        assert history["attempts"] == 1


class TestSpacedRepetition:
    """Интервалы повторения: чем увереннее ответ, тем дальше следующий показ."""

    async def _schedule(self, question=Q1):
        return (await db_module.get_schedule(STUDENT_1))[
            db_module.question_hash(question)
        ]

    async def test_wrong_answer_returns_question_immediately(self, env):
        await _answer(STUDENT_1, Q1, correct=False)
        assert await db_module.count_due(STUDENT_1) == 1

    async def test_correct_answer_postpones_question(self, env):
        await _answer(STUDENT_1, Q1, correct=True)
        assert await db_module.count_due(STUDENT_1) == 0

    async def test_interval_grows_with_streak(self, env):
        await _answer(STUDENT_1, Q1, correct=True)
        first = (await self._schedule())["due_at"]

        await _answer(STUDENT_1, Q1, correct=True)
        second = await self._schedule()

        assert second["streak"] == 2
        assert second["due_at"] > first

    async def test_mistake_resets_streak(self, env):
        for _ in range(3):
            await _answer(STUDENT_1, Q1, correct=True)
        assert (await self._schedule())["streak"] == 3

        await _answer(STUDENT_1, Q1, correct=False)
        state = await self._schedule()
        assert state["streak"] == 0
        assert await db_module.count_due(STUDENT_1) == 1

    async def test_streak_does_not_exceed_known_intervals(self, env):
        """Серия длиннее списка интервалов не должна ломать расчёт."""
        for _ in range(12):
            await _answer(STUDENT_1, Q1, correct=True)
        state = await self._schedule()
        assert state["streak"] == 12
        assert state["due_at"] is not None

    async def test_schedule_is_per_student(self, env):
        await _answer(STUDENT_1, Q1, correct=False)
        assert await db_module.count_due(STUDENT_2) == 0


class TestQuestionOrdering:
    """Порядок вопросов в тренировке: сначала просроченные, потом новые."""

    def _rows(self, *texts):
        return [{"Вопрос": t} for t in texts]

    def test_due_first_then_new_then_learned(self):
        from bot.handlers.tests import _sort_by_priority

        now = "2026-08-17T12:00:00"
        schedule = {
            db_module.question_hash("просрочен"): {
                "due_at": "2026-08-10T00:00:00", "streak": 0, "last_correct": False,
            },
            db_module.question_hash("освоен"): {
                "due_at": "2026-09-01T00:00:00", "streak": 3, "last_correct": True,
            },
        }
        rows = self._rows("освоен", "новый", "просрочен")
        ordered = [r["Вопрос"] for r in _sort_by_priority(rows, schedule, now)]
        assert ordered == ["просрочен", "новый", "освоен"]

    def test_repeats_take_at_most_half_the_session(self):
        """Ученик с горой ошибок всё равно должен видеть новый материал.

        Иначе тренировка превращается в бесконечное пережёвывание ошибок,
        а для этого есть отдельный режим.
        """
        from bot.handlers.tests import _sort_by_priority

        now = "2026-08-17T12:00:00"
        overdue = [f"ошибка {i}" for i in range(50)]
        new = [f"новый {i}" for i in range(50)]
        schedule = {
            db_module.question_hash(t): {
                "due_at": "2026-08-01T00:00:00", "streak": 0, "last_correct": False,
            }
            for t in overdue
        }

        ordered = _sort_by_priority(
            self._rows(*overdue, *new), schedule, now, limit=20
        )
        session = [r["Вопрос"] for r in ordered[:20]]
        repeats = sum(1 for q in session if q.startswith("ошибка"))
        assert repeats == 10
        assert len(session) - repeats == 10

    def test_oldest_repeats_come_first(self):
        """Копилка ошибок должна прокручиваться равномерно."""
        from bot.handlers.tests import _sort_by_priority

        now = "2026-08-17T12:00:00"
        schedule = {
            db_module.question_hash("давний"): {
                "due_at": "2026-07-01T00:00:00", "streak": 0, "last_correct": False,
            },
            db_module.question_hash("вчерашний"): {
                "due_at": "2026-08-16T00:00:00", "streak": 0, "last_correct": False,
            },
        }
        ordered = _sort_by_priority(
            self._rows("вчерашний", "давний"), schedule, now, limit=10
        )
        assert ordered[0]["Вопрос"] == "давний"

    def test_repeats_fill_the_gap_when_no_new_questions(self):
        """Если нового материала нет, добираем повторами, а не оставляем пусто."""
        from bot.handlers.tests import _sort_by_priority

        now = "2026-08-17T12:00:00"
        overdue = [f"ошибка {i}" for i in range(30)]
        schedule = {
            db_module.question_hash(t): {
                "due_at": "2026-08-01T00:00:00", "streak": 0, "last_correct": False,
            }
            for t in overdue
        }
        ordered = _sort_by_priority(self._rows(*overdue), schedule, now, limit=20)
        assert len(ordered[:20]) == 20

    def test_all_new_questions_are_kept(self):
        from bot.handlers.tests import _sort_by_priority

        rows = self._rows("а", "б", "в")
        ordered = _sort_by_priority(rows, {}, "2026-08-17T12:00:00")
        assert sorted(r["Вопрос"] for r in ordered) == ["а", "б", "в"]

    def test_nothing_is_lost(self):
        """Пересортировка не должна терять или дублировать вопросы."""
        from bot.handlers.tests import _sort_by_priority

        now = "2026-08-17T12:00:00"
        rows = self._rows(*[f"вопрос {i}" for i in range(20)])
        schedule = {
            db_module.question_hash("вопрос 3"): {
                "due_at": "2026-08-01T00:00:00", "streak": 0, "last_correct": False,
            },
            db_module.question_hash("вопрос 7"): {
                "due_at": "2026-12-01T00:00:00", "streak": 5, "last_correct": True,
            },
        }
        ordered = _sort_by_priority(rows, schedule, now)
        assert len(ordered) == 20
        assert {r["Вопрос"] for r in ordered} == {r["Вопрос"] for r in rows}


class TestStreak:
    async def test_no_answers_no_streak(self, env):
        assert await db_module.get_activity_streak(STUDENT_1) == 0

    async def test_answers_today_start_streak(self, env):
        await _answer(STUDENT_1, Q1, correct=True)
        assert await db_module.get_activity_streak(STUDENT_1) == 1

    async def test_streak_counts_consecutive_days(self, env):
        import aiosqlite
        from datetime import timedelta

        base = db_module._utcnow()
        async with aiosqlite.connect(db_module.DB_PATH) as db:
            for days_ago in (0, 1, 2, 5):
                await db.execute(
                    "INSERT INTO answer_log "
                    "(user_id, teacher_id, subject, question_hash, is_correct, "
                    " student_answer, answered_at) VALUES (?, ?, ?, ?, 1, '', ?)",
                    (STUDENT_1, TEACHER_A, SUBJECT, f"h{days_ago}",
                     (base - timedelta(days=days_ago)).isoformat()),
                )
            await db.commit()

        # Три дня подряд, а пятидневной давности запись серию не продолжает
        assert await db_module.get_activity_streak(STUDENT_1) == 3

    async def test_streak_breaks_after_two_days_off(self, env):
        import aiosqlite
        from datetime import timedelta

        old = (db_module._utcnow() - timedelta(days=3)).isoformat()
        async with aiosqlite.connect(db_module.DB_PATH) as db:
            await db.execute(
                "INSERT INTO answer_log "
                "(user_id, teacher_id, subject, question_hash, is_correct, "
                " student_answer, answered_at) VALUES (?, ?, ?, 'h', 1, '', ?)",
                (STUDENT_1, TEACHER_A, SUBJECT, old),
            )
            await db.commit()

        assert await db_module.get_activity_streak(STUDENT_1) == 0


class TestLaggingStudents:
    async def test_only_active_students_are_judged(self, env):
        """Две неудачные попытки не должны записывать ученика в отстающие."""
        from bot.handlers.menu import _lagging_students

        await db_module.ensure_user(STUDENT_1)
        await db_module.bind_student(STUDENT_1, TEACHER_A, SUBJECT)
        await _answer(STUDENT_1, Q1, correct=False)
        await _answer(STUDENT_1, Q2, correct=False)

        assert await _lagging_students(TEACHER_A, SUBJECT, class_accuracy=80) == []

    async def test_finds_student_below_class(self, env):
        from bot.handlers.menu import _lagging_students

        await db_module.ensure_user(STUDENT_1)
        await db_module.bind_student(STUDENT_1, TEACHER_A, SUBJECT)
        await db_module.set_profile(STUDENT_1, name="Ваня")
        for i in range(12):
            await _answer(STUDENT_1, f"вопрос {i}", correct=i < 3)

        lagging = await _lagging_students(TEACHER_A, SUBJECT, class_accuracy=80)
        assert [s["name"] for s in lagging] == ["Ваня"]

    async def test_strong_student_is_not_listed(self, env):
        from bot.handlers.menu import _lagging_students

        await db_module.ensure_user(STUDENT_2)
        await db_module.bind_student(STUDENT_2, TEACHER_A, SUBJECT)
        for i in range(12):
            await _answer(STUDENT_2, f"вопрос {i}", correct=True)

        assert await _lagging_students(TEACHER_A, SUBJECT, class_accuracy=80) == []


class TestHardQuestions:
    async def test_orders_by_number_of_errors(self, env):
        for _ in range(5):
            await _answer(STUDENT_1, Q1, correct=False)
        await _answer(STUDENT_2, Q2, correct=False)

        hard = await db_module.get_hard_questions(TEACHER_A)
        assert hard[0]["preview"].startswith("Какое событие")
        assert hard[0]["errors"] == 5

    async def test_fully_correct_questions_are_not_listed(self, env):
        await _answer(STUDENT_1, Q1, correct=True)
        assert await db_module.get_hard_questions(TEACHER_A) == []

    async def test_counts_distinct_students(self, env):
        await _answer(STUDENT_1, Q1, correct=False)
        await _answer(STUDENT_2, Q1, correct=False)

        hard = await db_module.get_hard_questions(TEACHER_A, min_errors=1)
        assert hard[0]["students"] == 2
        assert hard[0]["error_rate"] == 100

    async def test_isolated_between_teachers(self, env):
        """Преподаватель не должен видеть статистику по чужим вопросам."""
        await _answer(STUDENT_1, Q1, correct=False, teacher_id=TEACHER_A)
        await _answer(STUDENT_2, Q2, correct=False, teacher_id=TEACHER_B)

        hard_a = await db_module.get_hard_questions(TEACHER_A, min_errors=1)
        assert [h["preview"] for h in hard_a] == [Q1]

    async def test_single_error_is_not_reported(self, env):
        """Одна ошибка ничего не говорит — «1 из 1» в списке не нужен."""
        await _answer(STUDENT_1, Q1, correct=False)
        assert await db_module.get_hard_questions(TEACHER_A) == []

    async def test_three_errors_are_reported(self, env):
        for student in (STUDENT_1, STUDENT_2, 9703):
            await _answer(student, Q1, correct=False)

        hard = await db_module.get_hard_questions(TEACHER_A)
        assert hard[0]["errors"] == 3


class TestTeacherTotals:
    async def test_totals_across_students(self, env):
        await _answer(STUDENT_1, Q1, correct=True)
        await _answer(STUDENT_1, Q2, correct=False)
        await _answer(STUDENT_2, Q1, correct=True)

        totals = await db_module.get_teacher_totals(TEACHER_A)
        assert totals["attempts"] == 3
        assert totals["correct"] == 2
        assert totals["students"] == 2
        assert totals["accuracy"] == 67

    async def test_empty(self, env):
        totals = await db_module.get_teacher_totals(TEACHER_A)
        assert totals == {"attempts": 0, "correct": 0, "students": 0, "accuracy": 0}


class TestJournalPruning:
    async def test_counters_survive_pruning(self, env):
        """Главное свойство: чистка журнала не трогает статистику."""
        await _answer(STUDENT_1, Q1, correct=False)
        await _answer(STUDENT_1, Q1, correct=True)

        removed = await db_module.prune_answer_log(days=0)
        assert removed == 2

        progress = await db_module.get_student_progress(STUDENT_1)
        assert progress["attempts"] == 2
        assert progress["accuracy"] == 50

    async def test_recent_records_are_kept(self, env):
        await _answer(STUDENT_1, Q1, correct=True)
        assert await db_module.prune_answer_log(days=90) == 0


class TestRebindingKeepsStats:
    async def test_stats_are_not_cleared_on_rebinding(self, env):
        """Ошибки при смене преподавателя чистятся, а общая статистика — нет.

        Прогресс ученика — его собственный, он не принадлежит преподавателю.
        """
        await db_module.ensure_user(TEACHER_B)
        await db_module.set_role(TEACHER_B, ROLE_TEACHER)
        await db_module.ensure_user(STUDENT_1)
        await db_module.bind_student(STUDENT_1, TEACHER_A, SUBJECT)
        await _answer(STUDENT_1, Q1, correct=True)

        await db_module.bind_student(STUDENT_1, TEACHER_B, SUBJECT)
        assert (await db_module.get_student_progress(STUDENT_1))["attempts"] == 1
