"""Доступ преподавателя: пробный период, ручная выдача, лимит учеников.

Проверяется в первую очередь то, что стоит денег или доверия:

- **доступ ученика — это доступ его преподавателя.** Ученик не платит,
  и занятия у него останавливаются вместе с преподавательскими;
- **оплата находит человека по номеру**, в том числе выданная раньше,
  чем человек вообще зашёл в бота;
- **лимит жёсткий**, иначе тариф по числу учеников ничего не значит;
- **продление не съедает остаток** уже оплаченного или пробного срока.
"""

import datetime as dt
import shutil
import sys
import tempfile
from pathlib import Path

import pytest
import pytest_asyncio

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aiogram.types import CallbackQuery, Chat, Contact, Message  # noqa: E402
from aiogram.types import User as TgUser  # noqa: E402

import bot.access as access_mod  # noqa: E402
import database.db as db_module  # noqa: E402
from bot.handlers.subscription import (  # noqa: E402
    _request_card as request_card,
    person,
    split_message,
)
from bot.keyboards.inline import (  # noqa: E402
    MENU_BTN_BACK_TO_CABINET,
    MENU_BTN_SUBSCRIPTION,
    blocked_kb,
)
from database.models import ROLE_STUDENT, ROLE_TEACHER, Subscription  # noqa: E402
from services import subscription as sub_lib  # noqa: E402

TEACHER, OTHER_TEACHER = 9101, 9102
STUDENT = 9201
PHONE = "+375291234567"
SUBJECT = "history"


@pytest_asyncio.fixture
async def env():
    tmpdir = Path(tempfile.mkdtemp(prefix="aida_sub_"))
    saved = db_module.DB_PATH
    db_module.DB_PATH = str(tmpdir / "test.sqlite3")
    try:
        await db_module.init_db()
        yield tmpdir
    finally:
        db_module.DB_PATH = saved
        shutil.rmtree(tmpdir, ignore_errors=True)


async def _teacher(tid: int = TEACHER):
    await db_module.ensure_user(tid)
    await db_module.set_role(tid, ROLE_TEACHER)
    await db_module.set_subject(tid, SUBJECT)
    return await sub_lib.ensure_trial(tid)


async def _student(sid: int, teacher_id: int):
    await db_module.ensure_user(sid)
    await db_module.bind_student(sid, teacher_id, SUBJECT)


async def _expire(tid: int) -> None:
    """Отматывает все сроки в прошлое — как будто месяц кончился."""
    past = sub_lib._utcnow() - dt.timedelta(days=1)
    async with __import__("aiosqlite").connect(db_module.DB_PATH) as db:
        await db.execute(
            "UPDATE subscriptions SET trial_until = ?, paid_until = NULL WHERE tg_user_id = ?",
            (past.isoformat(), tid),
        )
        await db.commit()


class TestPhone:
    """Один номер человек напишет пятью способами, телеграм — шестым."""

    @pytest.mark.parametrize("raw", [
        "+375291234567",
        "375291234567",
        "80291234567",
        "+375 (29) 123-45-67",
        "291234567",
    ])
    def test_all_forms_give_one_number(self, raw):
        assert sub_lib.normalize_phone(raw) == PHONE

    def test_russian_number_is_not_mistaken_for_belarusian(self):
        # 8 перед десятью цифрами — российский формат, 80 — белорусский
        assert sub_lib.normalize_phone("89161234567") == "+79161234567"

    def test_empty_stays_empty(self):
        assert sub_lib.normalize_phone("") == ""
        assert sub_lib.normalize_phone("не номер") == ""

    def test_readable_form(self):
        assert sub_lib.format_phone(PHONE) == "+375 29 123-45-67"


class TestMonths:
    def test_month_end_does_not_overflow(self):
        # 31 января плюс месяц — 28 февраля, а не 3 марта
        start = dt.datetime(2027, 1, 31)
        assert sub_lib.add_months(start, 1) == dt.datetime(2027, 2, 28)

    def test_year_rolls_over(self):
        start = dt.datetime(2026, 10, 4)
        assert sub_lib.add_months(start, 6) == dt.datetime(2027, 4, 4)


class TestTrial:
    async def test_trial_starts_on_registration(self, env):
        access = await _teacher()
        assert access.active
        assert access.kind == sub_lib.KIND_TRIAL
        assert access.student_limit == sub_lib.TRIAL_STUDENT_LIMIT

    async def test_second_call_does_not_extend(self, env):
        first = await _teacher()
        again = await sub_lib.ensure_trial(TEACHER)
        assert again.until == first.until

    async def test_old_teacher_gets_four_months(self, env):
        """Кто пользовался ботом до подписки, не должен упереться в стену
        через месяц из-за нашей правки."""
        await db_module.ensure_user(TEACHER)
        await db_module.set_role(TEACHER, ROLE_TEACHER)
        # Подменяем дату регистрации на «до появления подписки»
        old = sub_lib.GRANDFATHER_BEFORE - dt.timedelta(days=20)
        async with __import__("aiosqlite").connect(db_module.DB_PATH) as db:
            await db.execute(
                "UPDATE users SET created_at = ? WHERE telegram_id = ?",
                (old.isoformat(), TEACHER),
            )
            await db.commit()

        access = await sub_lib.ensure_trial(TEACHER)

        expected = sub_lib.add_months(sub_lib._utcnow(), sub_lib.GRANDFATHER_MONTHS)
        assert access.until.date() == expected.date()
        assert access.days_left > 100

    async def test_newcomer_still_gets_one_month(self, env):
        access = await _teacher()
        assert access.days_left <= sub_lib.TRIAL_DAYS

    async def test_expired_trial_closes_access(self, env):
        await _teacher()
        await _expire(TEACHER)

        access = await sub_lib.access_for_teacher(TEACHER)
        assert not access.active
        assert access.kind == sub_lib.KIND_EXPIRED


class TestStudentInheritsAccess:
    """Ученик не платит: его занятия держатся на доступе преподавателя."""

    async def test_student_works_while_teacher_paid(self, env):
        await _teacher()
        await _student(STUDENT, TEACHER)

        access = await sub_lib.access_for_user(await db_module.get_user(STUDENT))
        assert access.active

    async def test_student_stops_with_teacher(self, env):
        await _teacher()
        await _student(STUDENT, TEACHER)
        await _expire(TEACHER)

        access = await sub_lib.access_for_user(await db_module.get_user(STUDENT))
        assert not access.active

    async def test_unbound_student_is_not_a_payment_question(self, env):
        await db_module.ensure_user(STUDENT)
        await db_module.set_role(STUDENT, ROLE_STUDENT)

        access = await sub_lib.access_for_user(await db_module.get_user(STUDENT))
        # Не «оплатите», а «ты ни к кому не привязан» — это разные разговоры
        assert access.kind == sub_lib.KIND_NONE


class TestGrant:
    async def test_grant_by_known_phone(self, env):
        await _teacher()
        await sub_lib.attach_phone(TEACHER, PHONE)

        result = await sub_lib.grant_by_phone(PHONE, months=6, student_limit=30)

        assert result.applied
        assert result.teacher_id == TEACHER
        assert result.access.kind == sub_lib.KIND_PAID
        assert result.access.student_limit == 30

    async def test_unknown_phone_is_stored_and_applied_later(self, env):
        # Оплата приходит раньше, чем человек зашёл в бота
        result = await sub_lib.grant_by_phone(PHONE, months=3, student_limit=30)
        assert not result.applied

        await _teacher()
        _phone, access = await sub_lib.attach_phone(TEACHER, "80291234567")

        assert access.kind == sub_lib.KIND_PAID
        assert access.student_limit == 30
        # Выдача сгорает после применения, второй раз не встанет
        assert await db_module.list_pending_grants() == []

    async def test_grant_does_not_eat_the_rest_of_trial(self, env):
        trial = await _teacher()
        await sub_lib.attach_phone(TEACHER, PHONE)

        result = await sub_lib.grant_by_phone(PHONE, months=1, student_limit=10)

        # Месяц отсчитывается от конца пробного, а не от сегодня
        assert result.access.until > trial.until
        assert result.access.until == sub_lib.add_months(trial.until, 1)

    async def test_repeated_pending_grant_replaces_previous(self, env):
        await sub_lib.grant_by_phone(PHONE, months=3, student_limit=30)
        await sub_lib.grant_by_phone(PHONE, months=6, student_limit=60)

        pending = await db_module.list_pending_grants()
        assert len(pending) == 1
        assert pending[0]["months"] == 6

    async def test_phone_belongs_to_one_teacher(self, env):
        await _teacher(TEACHER)
        await _teacher(OTHER_TEACHER)
        await sub_lib.attach_phone(TEACHER, PHONE)

        found = await db_module.get_subscription_by_phone(PHONE)
        assert found.tg_user_id == TEACHER


class TestStudentLimit:
    """Лимит жёсткий: мягкий превращает тариф в пожелание."""

    async def test_free_slot_lets_student_in(self, env):
        await _teacher()
        slot = await sub_lib.student_slot(TEACHER, STUDENT)
        assert slot.allowed

    async def test_full_tariff_stops_new_student(self, env):
        await _teacher()
        for i in range(sub_lib.TRIAL_STUDENT_LIMIT):
            await _student(9300 + i, TEACHER)

        slot = await sub_lib.student_slot(TEACHER, STUDENT)
        assert not slot.allowed
        assert slot.reason == "limit"
        assert (slot.count, slot.limit) == (10, 10)

    async def test_own_student_is_not_new(self, env):
        await _teacher()
        for i in range(sub_lib.TRIAL_STUDENT_LIMIT):
            await _student(9300 + i, TEACHER)

        # Уже подключённый переходит по той же ссылке ещё раз
        slot = await sub_lib.student_slot(TEACHER, 9300)
        assert slot.allowed

    async def test_expired_access_stops_new_student(self, env):
        await _teacher()
        await _expire(TEACHER)

        slot = await sub_lib.student_slot(TEACHER, STUDENT)
        assert not slot.allowed
        assert slot.reason == "expired"

    async def test_paid_tariff_raises_the_ceiling(self, env):
        await _teacher()
        await sub_lib.attach_phone(TEACHER, PHONE)
        for i in range(sub_lib.TRIAL_STUDENT_LIMIT):
            await _student(9300 + i, TEACHER)

        await sub_lib.grant_by_phone(PHONE, months=6, student_limit=30)

        slot = await sub_lib.student_slot(TEACHER, STUDENT)
        assert slot.allowed
        assert slot.limit == 30


class TestAccessKind:
    """Оплаченный и пробный сроки сравниваются, а не складываются."""

    def test_later_date_wins(self):
        now = dt.datetime(2026, 9, 10)
        sub = Subscription(
            tg_user_id=TEACHER,
            trial_until=now + dt.timedelta(days=20),
            paid_until=now + dt.timedelta(days=200),
        )
        access = sub_lib.access_of(sub, now=now)
        assert access.kind == sub_lib.KIND_PAID
        assert access.days_left == 200

    def test_trial_holds_while_it_is_longer(self):
        now = dt.datetime(2026, 9, 10)
        sub = Subscription(
            tg_user_id=TEACHER,
            trial_until=now + dt.timedelta(days=20),
            paid_until=now - dt.timedelta(days=5),
        )
        access = sub_lib.access_of(sub, now=now)
        assert access.active
        assert access.kind == sub_lib.KIND_TRIAL

    def test_no_subscription_is_no_access(self):
        assert sub_lib.access_of(None).kind == sub_lib.KIND_NONE


class TestPricing:
    """Цены считаются в одном месте — вариант Б из ЭКОНОМИКА.md."""

    def test_month_has_no_discount(self):
        assert sub_lib.term_price(30, 1) == 60

    def test_discounts_go_up_by_ten_per_step(self):
        assert sub_lib.term_price(30, 3) == round(60 * 3 * 0.9)
        assert sub_lib.term_price(30, 6) == round(60 * 6 * 0.8)
        assert sub_lib.term_price(100, 12) == round(150 * 12 * 0.7)

    def test_month_is_the_worst_deal(self):
        """Месяц — низкий вход, а не дешёвая дыра: в пересчёте на месяц
        он дороже любого длинного срока."""
        for limit, _price in sub_lib.TARIFFS:
            per_month = [
                sub_lib.term_price(limit, months) / months
                for months, _discount in sub_lib.TERMS
            ]
            assert per_month == sorted(per_month, reverse=True)

    def test_unknown_tariff_is_an_error_not_a_zero(self):
        # Иначе опечатка в кнопке молча продала бы доступ бесплатно
        with pytest.raises(ValueError):
            sub_lib.monthly_price(45)

    @pytest.mark.parametrize("students,expected", [
        (0, 10), (10, 10), (11, 30), (31, 60), (61, 100), (500, 100),
    ])
    def test_suggested_tariff_fits_the_person(self, students, expected):
        assert sub_lib.suggested_limit(students) == expected


class TestRenewalRequest:
    async def test_request_is_stored_and_closed_on_grant(self, env):
        await _teacher()
        await sub_lib.attach_phone(TEACHER, PHONE)
        await db_module.add_renewal_request(TEACHER, 30, 6, 306)

        open_now = await db_module.list_open_renewal_requests()
        assert len(open_now) == 1
        assert open_now[0]["price"] == 306

        # Выдали доступ — заявка больше не висит
        await sub_lib.grant_by_phone(PHONE, 6, 30)
        await db_module.close_renewal_requests(TEACHER)
        assert await db_module.list_open_renewal_requests() == []

    async def test_username_is_remembered_for_writing_back(self, env):
        await _teacher()
        await db_module.update_last_active(TEACHER, username="maria_p", tg_name="Мария")

        user = await db_module.get_user(TEACHER)
        assert user.username == "maria_p"
        assert "@maria_p" in person(user)

    async def test_empty_username_does_not_erase_the_old_one(self, env):
        await _teacher()
        await db_module.update_last_active(TEACHER, username="maria_p", tg_name="Мария")
        # Человек убрал @username из профиля — прежний остаётся в базе
        await db_module.update_last_active(TEACHER)

        user = await db_module.get_user(TEACHER)
        assert user.username == "maria_p"

    async def test_student_name_is_not_overwritten_by_telegram(self, env):
        await _teacher()
        await _student(STUDENT, TEACHER)
        await db_module.set_profile(STUDENT, name="Ваня")
        await db_module.update_last_active(STUDENT, tg_name="Иван Иванов")

        user = await db_module.get_user(STUDENT)
        # Анкета важнее профиля: имя ученик вводил сам
        assert user.name == "Ваня"
        assert user.tg_name == "Иван Иванов"


class TestAdminIsStrict:
    """Денежные команды запираются наглухо, когда админов не задали.

    В остальном проекте идиома обратная (`if ADMIN_IDS and ...` пускает
    всех при пустом списке) — для выдачи доступа так нельзя.
    """

    def test_empty_list_means_nobody(self, monkeypatch):
        monkeypatch.setattr(access_mod.config, "ADMIN_IDS", [])
        assert access_mod.is_admin(TEACHER) is False

    def test_only_listed_admins_pass(self, monkeypatch):
        monkeypatch.setattr(access_mod.config, "ADMIN_IDS", [TEACHER])
        assert access_mod.is_admin(TEACHER) is True
        assert access_mod.is_admin(OTHER_TEACHER) is False

    def test_nobody_bypasses_the_guard_either(self, monkeypatch):
        monkeypatch.setattr(access_mod.config, "ADMIN_IDS", [])
        assert access_mod.admin_bypasses(TEACHER) is False


class TestPhoneCollision:
    """Один номер — один кабинет, иначе оплата не знает, кому доставаться."""

    async def test_taken_phone_is_refused_not_crashed(self, env):
        await _teacher(TEACHER)
        await _teacher(OTHER_TEACHER)
        await sub_lib.attach_phone(TEACHER, PHONE)

        with pytest.raises(sub_lib.PhoneTaken):
            await sub_lib.attach_phone(OTHER_TEACHER, PHONE)

    async def test_refusal_leaves_everything_as_it_was(self, env):
        await _teacher(TEACHER)
        await _teacher(OTHER_TEACHER)
        await sub_lib.attach_phone(TEACHER, PHONE)
        # Оплата пришла на этот номер и ждёт хозяина
        await sub_lib.grant_by_phone(PHONE, months=6, student_limit=30)

        with pytest.raises(sub_lib.PhoneTaken):
            await sub_lib.attach_phone(OTHER_TEACHER, PHONE)

        # Номер остался у первого, чужой доступ не тронут
        assert (await db_module.get_subscription_by_phone(PHONE)).tg_user_id == TEACHER
        assert (await db_module.get_subscription(OTHER_TEACHER)).phone == ""

    async def test_own_phone_again_is_fine(self, env):
        await _teacher()
        await sub_lib.attach_phone(TEACHER, PHONE)
        # Тот же человек нажал «поделиться» ещё раз — это не столкновение
        phone, _access = await sub_lib.attach_phone(TEACHER, "80291234567")
        assert phone == PHONE


class TestRequestsClosedByAnyGrant:
    async def test_grant_closes_the_request(self, env):
        await _teacher()
        await sub_lib.attach_phone(TEACHER, PHONE)
        await db_module.add_renewal_request(TEACHER, 30, 6, 306)

        # Выдача командой, а не кнопкой из карточки
        await sub_lib.grant_by_phone(PHONE, months=6, student_limit=30)

        assert await db_module.list_open_renewal_requests() == []

    async def test_prepaid_grant_closes_it_too(self, env):
        await _teacher()
        await sub_lib.attach_phone(TEACHER, PHONE)
        await db_module.add_renewal_request(TEACHER, 30, 6, 306)

        # Человек оплатил, доступ выдан впрок на его номер — и встал сам
        await db_module.set_subscription_phone(TEACHER, "")
        await sub_lib.grant_by_phone(PHONE, months=6, student_limit=30)
        await sub_lib.attach_phone(TEACHER, PHONE)

        assert await db_module.list_open_renewal_requests() == []


class TestRequestScreen:
    """Экран заявок: ничего не должно потеряться и повиснуть."""

    async def test_new_request_replaces_the_previous_one(self, env):
        await _teacher()
        await db_module.add_renewal_request(TEACHER, 10, 1, 25)
        await db_module.add_renewal_request(TEACHER, 30, 6, 288)

        open_now = await db_module.list_open_renewal_requests()
        # Трижды нажатая кнопка не должна давать трёх одинаковых карточек
        assert len(open_now) == 1
        assert open_now[0]["months"] == 6

    async def test_requests_of_different_people_live_side_by_side(self, env):
        await _teacher(TEACHER)
        await _teacher(OTHER_TEACHER)
        await db_module.add_renewal_request(TEACHER, 10, 1, 25)
        await db_module.add_renewal_request(OTHER_TEACHER, 30, 6, 288)

        assert len(await db_module.list_open_renewal_requests()) == 2

    async def test_closing_without_granting(self, env):
        await _teacher()
        await sub_lib.attach_phone(TEACHER, PHONE)
        await db_module.add_renewal_request(TEACHER, 30, 6, 288)
        request_id = (await db_module.list_open_renewal_requests())[0]["id"]

        assert await db_module.close_renewal_request(request_id) is True
        assert await db_module.list_open_renewal_requests() == []
        # Доступ при этом не выдан: закрыли, а не продлили
        assert (await sub_lib.access_for_teacher(TEACHER)).kind == sub_lib.KIND_TRIAL

    async def test_closing_twice_is_not_an_error(self, env):
        await _teacher()
        await db_module.add_renewal_request(TEACHER, 30, 6, 288)
        request_id = (await db_module.list_open_renewal_requests())[0]["id"]

        await db_module.close_renewal_request(request_id)
        # Кнопка живёт в чате вечно, нажать её повторно — обычное дело
        assert await db_module.close_renewal_request(request_id) is False

    async def test_card_shows_the_person_and_what_they_asked(self, env):
        await _teacher()
        await db_module.update_last_active(TEACHER, username="maria_p", tg_name="Мария")
        await sub_lib.attach_phone(TEACHER, PHONE)
        await db_module.add_renewal_request(TEACHER, 30, 6, 288)

        text, markup = await request_card(0)

        assert "Мария · @maria_p" in text
        assert "+375 29 123-45-67" in text
        assert "До 30 учеников · 6 месяцев · 288 BYN" in text
        assert markup is not None

    async def test_empty_list_says_so(self, env):
        text, markup = await request_card(0)
        assert "Заявок нет" in text
        assert markup is None

    async def test_position_beyond_the_list_does_not_crash(self, env):
        await _teacher()
        await db_module.add_renewal_request(TEACHER, 30, 6, 288)
        # Кнопка листалки из старого сообщения, когда заявок было больше
        text, _markup = await request_card(7)
        assert "До 30 учеников" in text


class TestGuard:
    """Заслонка на входе: с истёкшим доступом дальше не пускают.

    Отдельно проверяется обратное — что дорога к продлению остаётся
    открытой. Запертый экран подписки означал бы, что оплатить нельзя.
    """

    @staticmethod
    def _message(text: str = "", contact=None):
        return Message(
            message_id=1,
            date=dt.datetime(2026, 9, 10),
            chat=Chat(id=TEACHER, type="private"),
            from_user=TgUser(id=TEACHER, is_bot=False, first_name="Мария"),
            text=text or None,
            contact=contact,
        )

    def test_subscription_screen_stays_open(self):
        assert access_mod._passes_without_access(self._message(MENU_BTN_SUBSCRIPTION))

    def test_commands_stay_open(self):
        assert access_mod._passes_without_access(self._message("/start"))

    def test_shared_phone_gets_through(self):
        contact = Contact(phone_number="+375291234567", first_name="Мария", user_id=TEACHER)
        assert access_mod._passes_without_access(self._message(contact=contact))

    def test_ordinary_button_does_not(self):
        assert not access_mod._passes_without_access(self._message("📥 Добавить вопросы"))

    async def test_expired_teacher_is_stopped(self, env, monkeypatch):
        monkeypatch.setattr(access_mod.config, "ADMIN_IDS", [])
        await _teacher()
        await _expire(TEACHER)

        guard, calls = _recording_guard()
        passed = await guard(
            _handler(calls),
            self._message("📥 Добавить вопросы"),
            {"event_from_user": TgUser(id=TEACHER, is_bot=False, first_name="Мария")},
        )

        assert passed is None
        assert calls == ["refused"]

    async def test_active_teacher_goes_through(self, env, monkeypatch):
        monkeypatch.setattr(access_mod.config, "ADMIN_IDS", [])
        await _teacher()

        guard, calls = _recording_guard()
        await guard(
            _handler(calls),
            self._message("📥 Добавить вопросы"),
            {"event_from_user": TgUser(id=TEACHER, is_bot=False, first_name="Мария")},
        )

        assert calls == ["handled"]

    async def test_teacher_without_record_gets_a_trial_not_free_pass(self, env, monkeypatch):
        monkeypatch.setattr(access_mod.config, "ADMIN_IDS", [])
        # Так выглядят все, кто зарегистрировался до появления подписки
        await db_module.ensure_user(TEACHER)
        await db_module.set_role(TEACHER, ROLE_TEACHER)
        assert await db_module.get_subscription(TEACHER) is None

        guard, calls = _recording_guard()
        await guard(
            _handler(calls),
            self._message("📥 Добавить вопросы"),
            {"event_from_user": TgUser(id=TEACHER, is_bot=False, first_name="Мария")},
        )

        assert calls == ["handled"]
        access = await sub_lib.access_for_teacher(TEACHER)
        assert access.kind == sub_lib.KIND_TRIAL

    async def test_unregistered_is_not_stopped(self, env, monkeypatch):
        monkeypatch.setattr(access_mod.config, "ADMIN_IDS", [])
        # Онбординг перекрывать нечем: доступа у человека ещё нет по определению
        guard, calls = _recording_guard()
        await guard(
            _handler(calls),
            self._message("что-то"),
            {"event_from_user": TgUser(id=777001, is_bot=False, first_name="Гость")},
        )

        assert calls == ["handled"]


class TestNoDeadEnds:
    """Из блокировки должен быть выход, чем бы человек ни пользовался."""

    def test_back_to_cabinet_passes_the_guard(self):
        # Преподавателя доступ мог застать в режиме ученика, а там кнопки
        # «Подписка» на клавиатуре нет
        message = TestGuard._message(MENU_BTN_BACK_TO_CABINET)
        assert access_mod._passes_without_access(message)

    def test_refusal_carries_a_button_to_subscription(self):
        kb = blocked_kb()
        assert kb.inline_keyboard[0][0].callback_data == "sub:open"
        # И эта кнопка сама проходит заслонку, иначе она бесполезна
        callback = CallbackQuery(
            id="1",
            from_user=TgUser(id=TEACHER, is_bot=False, first_name="Мария"),
            chat_instance="x",
            data="sub:open",
        )
        assert access_mod._passes_without_access(callback)

    def test_refusal_text_does_not_point_at_a_missing_button(self):
        text = access_mod.teacher_blocked(dt.datetime(2026, 10, 4))
        assert "откройте" not in text.lower()


class TestLongLists:
    """Список на сорок преподавателей телеграм не примет целиком."""

    def test_long_list_is_split(self):
        lines = [f"строка номер {i} с каким-то содержимым" for i in range(400)]
        chunks = split_message(lines)

        assert len(chunks) > 1
        assert all(len(c) <= 3500 for c in chunks)
        # Ничего не потерялось и порядок не сбился
        assert "\n".join(chunks) == "\n".join(lines)

    def test_short_list_stays_one_message(self):
        assert len(split_message(["одна", "две"])) == 1

    def test_overlong_line_is_not_cut_in_half(self):
        huge = "x" * 5000
        chunks = split_message(["до", huge, "после"])
        assert huge in chunks


class TestRehearsal:
    """Репетиция состояний: /sub_test и снятие админского обхода."""

    def test_rehearsal_buttons_survive_the_guard(self):
        # Иначе из состояния «доступ кончился» кнопками не выбраться
        for data in ("sub:test:trial30", "sub:test:expired", "sub:test:guard"):
            callback = CallbackQuery(
                id="1",
                from_user=TgUser(id=TEACHER, is_bot=False, first_name="Слава"),
                chat_instance="x",
                data=data,
            )
            assert access_mod._passes_without_access(callback)

    def test_guard_toggles_for_one_admin(self, monkeypatch):
        monkeypatch.setattr(access_mod.config, "ADMIN_IDS", [TEACHER])
        monkeypatch.setattr(access_mod, "GUARD_ON_ADMINS", set())

        assert access_mod.admin_bypasses(TEACHER)
        assert access_mod.toggle_admin_guard(TEACHER) is True
        assert not access_mod.admin_bypasses(TEACHER)
        # Другого админа это не касается
        assert access_mod.admin_bypasses(OTHER_TEACHER) is False
        access_mod.toggle_admin_guard(TEACHER)
        assert access_mod.admin_bypasses(TEACHER)

    @pytest.mark.parametrize("state,kind", [
        ("trial30", sub_lib.KIND_TRIAL),
        ("trial2", sub_lib.KIND_TRIAL),
        ("expired", sub_lib.KIND_EXPIRED),
        ("paid6", sub_lib.KIND_PAID),
    ])
    async def test_each_state_is_reachable(self, env, state, kind):
        from bot.handlers.subscription import _TEST_STATES

        await _teacher()
        _label, trial_days, paid_months, limit = _TEST_STATES[state]
        now = sub_lib._utcnow()
        await db_module.set_subscription_dates(
            TEACHER,
            now + dt.timedelta(days=trial_days),
            sub_lib.add_months(now, paid_months) if paid_months else None,
            limit,
        )

        access = await sub_lib.access_for_teacher(TEACHER)
        assert access.kind == kind


def _handler(calls):
    async def handler(event, data):
        calls.append("handled")
        return "handled"
    return handler


def _recording_guard():
    """Заслонка, которая вместо отправки сообщения записывает отказ."""
    calls: list[str] = []

    class Recording(access_mod.SubscriptionMiddleware):
        async def _refuse(self, event, is_teacher, access):
            calls.append("refused")

    return Recording(), calls
