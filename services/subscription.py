"""Доступ преподавателя: пробный период, оплата, лимит учеников.

**Единственное место, где решается, работает бот у человека или нет.**
Хендлеры спрашивают `access_for_user` и не знают ни про пробный период,
ни про даты, ни про то, что у ученика доступ чужой — преподавательский.

Подписка на человека, а не на предмет: расходы зависят от числа учеников,
а ученик по русскому стоит ровно столько же, сколько по истории.

Оплата приходит раньше, чем человек нажимает кнопку в боте, а иногда
и раньше, чем он вообще заходит. Поэтому выдача идёт по номеру телефона:
номер — общий ключ с внешней кассой, и выдать доступ можно на номер,
которого в базе ещё нет (`pending_grants`).
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Optional

from database.db import (
    add_pending_grant,
    close_renewal_requests,
    count_teacher_students,
    create_subscription,
    get_subscription,
    get_subscription_by_phone,
    get_user,
    set_subscription_paid,
    set_subscription_phone,
    take_pending_grant,
)
from database.models import ROLE_TEACHER, Subscription, User


logger = logging.getLogger(__name__)


# Пробный период — месяц. Решено 02.09.2026: вечный бесплатный тариф
# не создаёт момента оплаты никогда, ограниченный создаёт его в конкретный день.
TRIAL_DAYS = 30

# Сколько учеников можно подключить на пробном — как на минимальном тарифе.
# Обкатать с группой хватает, а сотню учеников бесплатно никто не заведёт.
TRIAL_STUDENT_LIMIT = 10

# Кто зарегистрировался до появления подписки — получает четыре месяца,
# а не месяц. Эти люди пользовались ботом, когда никакого срока не было,
# и упереться в стену через месяц из-за нашей правки они не должны.
# Дата сравнивается с `users.created_at`, поэтому правило срабатывает
# и через полгода — для того, кто всё это время не заходил.
GRANDFATHER_BEFORE = dt.datetime(2026, 9, 10)
GRANDFATHER_MONTHS = 4

# Лимит по умолчанию при ручной выдаче, если в команде не указан другой.
DEFAULT_GRANT_LIMIT = 30

# ---------- Тарифы ----------
#
# Вариант Б из `ЭКОНОМИКА.md`, выбран 10.09.2026. Цена за месяц при полной
# загрузке тарифа; себестоимость самого дорогого — 18 руб в месяц, так что
# запас есть у всех ступеней. Менять цифры можно прямо здесь: экран,
# кнопки и заявка считаются отсюда.
TARIFFS = (
    (10, 25),
    (30, 60),
    (60, 100),
    (100, 150),
)

# Срок и скидка за него — ровная лестница по десять процентов на ступень
# (решено 11.09.2026). Месяц идёт без скидки и потому самый невыгодный:
# он нужен как низкий вход для тех, кто ещё не доверяет сервису, но не должен
# быть дешёвой дырой, в которой люди остаются навсегда.
#
# Девятимесячного нет сознательно: его возьмут все и будут простаивать летом.
# Год со скидкой равен всему сроку жизни клиента — деньги те же, но сразу.
TERMS = (
    (1, 0),
    (3, 10),
    (6, 20),
    (12, 30),
)


def tariff_limits() -> tuple[int, ...]:
    return tuple(limit for limit, _price in TARIFFS)


def monthly_price(student_limit: int) -> int:
    for limit, price in TARIFFS:
        if limit == student_limit:
            return price
    raise ValueError(f"Нет тарифа на {student_limit} учеников")


def term_price(student_limit: int, months: int) -> int:
    """Цена за весь срок со скидкой, в целых рублях."""
    discount = dict(TERMS).get(months, 0)
    full = monthly_price(student_limit) * months
    return round(full * (100 - discount) / 100)


# Валюта пишется международным кодом: «руб» в Беларуси читается и как
# российские рубли, а разница между ними тридцатикратная. Строка одна
# на все экраны — кнопки, заявку и списки.
CURRENCY = "BYN"


def money(amount: int) -> str:
    return f"{amount} {CURRENCY}"


def tariff_label(student_limit: int) -> str:
    return f"до {student_limit} учеников"


def months_word(n: int) -> str:
    if 11 <= n % 100 <= 14:
        return "месяцев"
    return {1: "месяц", 2: "месяца", 3: "месяца", 4: "месяца"}.get(n % 10, "месяцев")


def suggested_limit(students: int) -> int:
    """Какой тариф подсветить: ближайший, куда человек помещается."""
    for limit, _price in TARIFFS:
        if students <= limit:
            return limit
    return TARIFFS[-1][0]


# Виды доступа.
KIND_TRIAL = "trial"
KIND_PAID = "paid"
KIND_EXPIRED = "expired"
KIND_NONE = "none"


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


@dataclass(slots=True)
class Access:
    """Ответ на единственный вопрос: работает бот у этого человека или нет."""

    active: bool
    kind: str
    until: Optional[dt.datetime] = None
    days_left: int = 0
    student_limit: int = 0
    phone: str = ""

    @property
    def is_trial(self) -> bool:
        return self.kind == KIND_TRIAL


NO_ACCESS = Access(active=False, kind=KIND_NONE)


# ---------- Номер телефона ----------

def normalize_phone(raw: str) -> str:
    """Приводит номер к виду «+375291234567».

    Один и тот же человек напишет номер пятью способами, а телеграм отдаёт
    его шестым — без плюса. Сравнивать их можно только после приведения
    к одному виду, иначе оплата не найдёт своего преподавателя.

    Разбор местных форматов: `80…` — белорусский междугородний префикс,
    одинокая `8` перед десятью цифрами — российский. Код российского
    оператора с нуля не начинается, поэтому эти два случая не путаются.
    """
    digits = "".join(ch for ch in (raw or "") if ch.isdigit())
    if not digits:
        return ""

    if digits.startswith("375"):
        pass
    elif digits.startswith("80") and len(digits) == 11:
        digits = "375" + digits[2:]
    elif digits.startswith("8") and len(digits) == 11:
        digits = "7" + digits[1:]
    elif len(digits) == 9:
        # Внутренний белорусский номер без кода страны: 29 123-45-67
        digits = "375" + digits

    return "+" + digits


_MONTHS = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)


def ru_date(value: Optional[dt.datetime]) -> str:
    """«4 октября 2026». Даты доступа читают люди, а не машины."""
    if not value:
        return "—"
    return f"{value.day} {_MONTHS[value.month - 1]} {value.year}"


def days_word(n: int) -> str:
    if 11 <= n % 100 <= 14:
        return "дней"
    return {1: "день", 2: "дня", 3: "дня", 4: "дня"}.get(n % 10, "дней")


def format_phone(phone: str) -> str:
    """Человеческий вид: +375 29 123-45-67. Незнакомый формат — как есть."""
    if phone.startswith("+375") and len(phone) == 13:
        return f"+375 {phone[4:6]} {phone[6:9]}-{phone[9:11]}-{phone[11:]}"
    return phone


# ---------- Даты ----------

def add_months(start: dt.datetime, months: int) -> dt.datetime:
    """Прибавляет месяцы по календарю: оплата на месяц — это до того же числа."""
    month_index = start.month - 1 + months
    year = start.year + month_index // 12
    month = month_index % 12 + 1
    # 31 января плюс месяц — это 28 (или 29) февраля, а не 3 марта
    day = min(start.day, _days_in_month(year, month))
    return start.replace(year=year, month=month, day=day)


def _days_in_month(year: int, month: int) -> int:
    if month == 12:
        return 31
    return (dt.date(year, month + 1, 1) - dt.date(year, month, 1)).days


def _days_left(until: Optional[dt.datetime], now: dt.datetime) -> int:
    if not until:
        return 0
    return max(0, (until.date() - now.date()).days)


# ---------- Доступ ----------

def access_of(sub: Optional[Subscription], now: Optional[dt.datetime] = None) -> Access:
    """Превращает запись в базе в ответ «работает или нет».

    Оплаченный срок и пробный сравниваются, а не складываются: действует
    тот, который заканчивается позже. Иначе оплата в первый же день
    пробного периода отнимала бы у человека остаток месяца.
    """
    if sub is None:
        return NO_ACCESS

    now = now or _utcnow()
    ends = [(sub.paid_until, KIND_PAID), (sub.trial_until, KIND_TRIAL)]
    ends = [(until, kind) for until, kind in ends if until]
    if not ends:
        return Access(active=False, kind=KIND_EXPIRED, phone=sub.phone,
                      student_limit=sub.student_limit)

    until, kind = max(ends, key=lambda pair: pair[0])
    active = now < until
    return Access(
        active=active,
        kind=kind if active else KIND_EXPIRED,
        until=until,
        days_left=_days_left(until, now),
        student_limit=sub.student_limit,
        phone=sub.phone,
    )


async def access_for_teacher(teacher_id: int) -> Access:
    return access_of(await get_subscription(teacher_id))


async def access_for_user(user: Optional[User]) -> Access:
    """Доступ пользователя. У ученика он не свой, а его преподавателя.

    Ученик за бота не платит и подписки не имеет: занятия у него
    останавливаются вместе с доступом преподавателя.
    """
    if user is None or not user.role:
        return NO_ACCESS
    if user.role == ROLE_TEACHER:
        return await access_for_teacher(user.telegram_id)
    if user.teacher_id:
        return await access_for_teacher(user.teacher_id)
    return NO_ACCESS


@dataclass(slots=True)
class StudentSlot:
    """Есть ли у преподавателя место для ещё одного ученика."""

    allowed: bool
    # Почему нет: «expired» — доступ кончился, «limit» — тариф заполнен.
    reason: str = ""
    count: int = 0
    limit: int = 0


async def student_slot(teacher_id: int, student_id: Optional[int] = None) -> StudentSlot:
    """Пускать ли к преподавателю нового ученика.

    Лимит жёсткий: мягкий превращает тариф по числу учеников в пожелание.
    Уже подключённого это не касается — он не «новый», и повторный переход
    по той же ссылке не должен упираться в стену.
    """
    access = await access_for_teacher(teacher_id)
    count = await count_teacher_students(teacher_id)
    limit = access.student_limit or TRIAL_STUDENT_LIMIT

    if student_id is not None:
        existing = await get_user(student_id)
        if existing and existing.teacher_id == teacher_id:
            return StudentSlot(allowed=True, count=count, limit=limit)

    if not access.active:
        return StudentSlot(allowed=False, reason="expired", count=count, limit=limit)
    if count >= limit:
        return StudentSlot(allowed=False, reason="limit", count=count, limit=limit)
    return StudentSlot(allowed=True, count=count, limit=limit)


async def ensure_trial(tg_user_id: int) -> Access:
    """Заводит пробный период. Повторный вызов срок не продлевает."""
    sub = await get_subscription(tg_user_id)
    if sub is None:
        trial_until = await _first_term(tg_user_id)
        sub = await create_subscription(tg_user_id, trial_until, TRIAL_STUDENT_LIMIT)
        logger.info("Trial started for %s until %s", tg_user_id, trial_until.date())
    return access_of(sub)


async def _first_term(tg_user_id: int) -> dt.datetime:
    """До какого числа открыт доступ у того, кто получает его впервые.

    Новичку — месяц. Тому, кто завёлся в боте до появления подписки, —
    четыре: срок ему меняем мы, а не он.
    """
    now = _utcnow()
    user = await get_user(tg_user_id)
    registered = user.created_at if user else None
    if registered and registered < GRANDFATHER_BEFORE:
        return add_months(now, GRANDFATHER_MONTHS)
    return now + dt.timedelta(days=TRIAL_DAYS)


# ---------- Привязка номера и выдача ----------

@dataclass(slots=True)
class GrantResult:
    """Что получилось из выдачи доступа."""

    phone: str
    months: int
    student_limit: int
    # Кому легло. None — номера в базе нет, выдача записана впрок.
    teacher_id: Optional[int] = None
    access: Optional[Access] = None

    @property
    def applied(self) -> bool:
        return self.teacher_id is not None


async def _extend(sub: Subscription, months: int, student_limit: int) -> Access:
    """Продлевает доступ от того срока, который есть, а не от сегодня.

    Оплата в середине пробного периода не должна съедать его остаток:
    считаем от более поздней из дат.
    """
    now = _utcnow()
    base = max([d for d in (now, sub.paid_until, sub.trial_until) if d])
    paid_until = add_months(base, months)
    await set_subscription_paid(sub.tg_user_id, paid_until, student_limit)
    # Заявка закрывается здесь, а не в хендлере: выдать доступ можно
    # кнопкой из карточки, командой `/grant` и оплатой впрок, и человек,
    # получивший доступ, не должен остаться в списке ждущих ни в одном
    # из трёх случаев
    await close_renewal_requests(sub.tg_user_id)
    logger.info(
        "Access granted: %s until %s, limit %s",
        sub.tg_user_id, paid_until.date(), student_limit,
    )
    return access_of(await get_subscription(sub.tg_user_id))


async def grant_by_phone(
    raw_phone: str, months: int, student_limit: int = DEFAULT_GRANT_LIMIT
) -> GrantResult:
    """Выдаёт доступ по номеру. Незнакомый номер — не отказ, а выдача впрок."""
    phone = normalize_phone(raw_phone)
    sub = await get_subscription_by_phone(phone)
    if sub is None:
        await add_pending_grant(phone, months, student_limit)
        logger.info("Pending grant stored for %s: %s months", phone, months)
        return GrantResult(phone=phone, months=months, student_limit=student_limit)

    access = await _extend(sub, months, student_limit)
    return GrantResult(
        phone=phone,
        months=months,
        student_limit=student_limit,
        teacher_id=sub.tg_user_id,
        access=access,
    )


class PhoneTaken(Exception):
    """Номер уже принадлежит другому кабинету.

    Не «не получилось сохранить», а именно занят: два кабинета на один
    номер означали бы, что оплата не знает, кому доставаться.
    """


async def attach_phone(tg_user_id: int, raw_phone: str) -> tuple[str, Access]:
    """Привязывает номер к преподавателю и подхватывает выдачу впрок.

    Порядок именно такой: сначала номер сохраняется, потом ищется оплата
    на него. Человек, оплативший до первого захода в бота, получает доступ
    в тот момент, когда делится номером, и ничего больше делать не должен.
    """
    phone = normalize_phone(raw_phone)
    if not phone:
        return "", await access_for_teacher(tg_user_id)

    # Проверяем до записи: иначе запрет в базе рвал бы обработчик молча,
    # а номер, записанный раньше поиска оплаты, увёл бы чужую выдачу впрок
    owner = await get_subscription_by_phone(phone)
    if owner is not None and owner.tg_user_id != tg_user_id:
        logger.info("Phone %s already belongs to %s", phone, owner.tg_user_id)
        raise PhoneTaken(phone)

    await ensure_trial(tg_user_id)
    try:
        await set_subscription_phone(tg_user_id, phone)
    except Exception as exc:  # noqa: BLE001
        # Гонка между проверкой и записью: два человека делятся одним
        # номером одновременно. Запрет в базе — последнее слово
        raise PhoneTaken(phone) from exc

    pending = await take_pending_grant(phone)
    if pending:
        sub = await get_subscription(tg_user_id)
        assert sub is not None
        access = await _extend(sub, pending["months"], pending["student_limit"])
        logger.info("Pending grant applied to %s (%s)", tg_user_id, phone)
        return phone, access

    return phone, await access_for_teacher(tg_user_id)
