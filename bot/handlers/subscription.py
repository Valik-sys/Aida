"""Экран подписки у преподавателя и ручная выдача доступа.

Ключ ко всему — номер телефона: по нему оплата находит человека, и по нему же
потом будет работать внешняя касса. Номер берём кнопкой «поделиться», а не
вводом руками: телеграм отдаёт подтверждённый номер, его нельзя опечатать.

Ручная выдача — не времянка на первые продажи. Она нужна всегда: вернуть
доступ после сбоя, открыть демо, выдать бонус. Автоматическая оплата потом
делает ровно то же самое, только дату ставит не человек.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import List

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import config
from bot import access as access_mod
from bot.keyboards.inline import (
    BTN_CANCEL_SHARE,
    MENU_BTN_SUBSCRIPTION,
    renew_confirm_kb,
    renew_limits_kb,
    renew_terms_kb,
    request_card_kb,
    share_phone_kb,
    sub_test_kb,
    subscription_kb,
    teacher_cabinet_kb,
)
from database.db import (
    add_renewal_request,
    close_renewal_request,
    count_teacher_students,
    get_subscription,
    get_user,
    list_open_renewal_requests,
    list_pending_grants,
    list_subscriptions,
    set_subscription_dates,
)
from database.models import ROLE_TEACHER
from services import subscription as sub_lib
from services.subscription import days_word, ru_date


logger = logging.getLogger(__name__)

router = Router()


# Телеграм не принимает сообщения длиннее 4096 символов и отвечает ошибкой,
# то есть длинный список не обрежется, а не придёт вовсе. Порог с запасом:
# в строках есть эмодзи, и каждый весит больше одного символа.
MESSAGE_LIMIT = 3500


def split_message(lines: List[str], limit: int = MESSAGE_LIMIT) -> List[str]:
    """Режет список строк на сообщения, не разрывая строку пополам.

    Одна строка длиннее предела уезжает в отдельное сообщение как есть:
    рвать её посередине хуже, чем отправить длинной.
    """
    chunks: List[str] = []
    current: List[str] = []
    size = 0
    for line in lines:
        if current and size + len(line) + 1 > limit:
            chunks.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


def person(user) -> str:
    """Как назвать человека в списке и в заявке."""
    if user is None:
        return "неизвестный"
    name = user.name or user.tg_name
    handle = f" · @{user.username}" if user.username else ""
    return (name or f"ID {user.telegram_id}") + handle


async def subscription_text(teacher_id: int) -> str:
    """Экран подписки. Три состояния: пробный, оплачено, кончилось."""
    access = await sub_lib.access_for_teacher(teacher_id)
    students = await count_teacher_students(teacher_id)
    limit = access.student_limit or sub_lib.TRIAL_STUDENT_LIMIT

    lines = ["💳 Подписка", ""]

    if access.kind == sub_lib.KIND_TRIAL:
        lines.append(
            f"Пробный период до {ru_date(access.until)} — "
            f"осталось {access.days_left} {days_word(access.days_left)}."
        )
        lines.append(f"Учеников: {students} из {limit}.")
    elif access.kind == sub_lib.KIND_PAID:
        lines.append(
            f"Оплачено до {ru_date(access.until)} — "
            f"осталось {access.days_left} {days_word(access.days_left)}."
        )
        lines.append(f"Учеников: {students} из {limit}.")
    else:
        lines.append(f"Доступ закончился {ru_date(access.until)}.")
        lines.append("Занятия остановлены — у вас и у ваших учеников.")
        lines.append("Материалы сохранены полностью, ничего не потеряно.")

    if access.phone:
        lines.append("")
        lines.append(f"Номер: {sub_lib.format_phone(access.phone)}")
    else:
        lines.append("")
        lines.append(
            "Номер телефона не привязан — по нему оплата находит ваш кабинет."
        )

    return "\n".join(lines)


async def show_subscription(message: Message, teacher_id: int) -> None:
    access = await sub_lib.access_for_teacher(teacher_id)
    # Клавиатуру не пересылаем: экран режима не меняет
    await message.answer(
        await subscription_text(teacher_id),
        reply_markup=subscription_kb(bool(access.phone)),
    )


@router.message(F.text == MENU_BTN_SUBSCRIPTION)
async def menu_subscription(message: Message, state: FSMContext) -> None:
    await state.clear()
    user = await get_user(message.from_user.id)
    if not user or user.role != ROLE_TEACHER:
        await message.answer("Этот раздел доступен преподавателям.")
        return
    await sub_lib.ensure_trial(user.telegram_id)
    await show_subscription(message, user.telegram_id)


# ---------- Продление: тариф → срок → заявка ----------
#
# Выбор едет в самих кнопках (`sub:ren:term:30:6`), а не в состоянии на
# сервере: перезапуск бота посреди выбора ничего не рвёт, а вернуться
# на шаг назад можно старой кнопкой из переписки.

def _connected_now(students: int) -> str:
    if not students:
        return "Пока не подключён ни один."
    if students == 1:
        return "Сейчас подключён 1."
    return f"Сейчас подключено {students}."


async def _renew_root(message: Message, teacher_id: int) -> None:
    students = await count_teacher_students(teacher_id)
    await message.answer(
        "💳 Продление доступа\n"
        "Шаг 1 из 2 — тариф\n\n"
        "Плата только за число учеников. Занятия, вопросы "
        "и материалы — без ограничений.\n\n"
        "Сколько учеников будет заниматься у вас в боте?\n"
        f"{_connected_now(students)}",
        reply_markup=renew_limits_kb(
            sub_lib.TARIFFS, sub_lib.suggested_limit(students)
        ),
    )


@router.callback_query(F.data == "sub:ren:root")
async def renew_root(callback: CallbackQuery) -> None:
    await callback.answer()
    await _renew_root(callback.message, callback.from_user.id)


@router.callback_query(F.data.startswith("sub:ren:lim:"))
async def renew_limit(callback: CallbackQuery) -> None:
    limit = int(callback.data.rsplit(":", 1)[-1])
    await callback.answer()

    terms = [
        (months, sub_lib.term_price(limit, months), discount)
        for months, discount in sub_lib.TERMS
    ]
    await callback.message.answer(
        f"Тариф «{sub_lib.tariff_label(limit)}».\n"
        f"{sub_lib.money(sub_lib.monthly_price(limit))} в месяц.\n\n"
        "На какой срок продлеваем?",
        reply_markup=renew_terms_kb(limit, terms),
    )


@router.callback_query(F.data.startswith("sub:ren:term:"))
async def renew_term(callback: CallbackQuery) -> None:
    _, _, _, raw_limit, raw_months = callback.data.split(":")
    limit, months = int(raw_limit), int(raw_months)
    await callback.answer()

    price = sub_lib.term_price(limit, months)
    await callback.message.answer(
        f"{sub_lib.tariff_label(limit).capitalize()}, "
        f"{months} {sub_lib.months_word(months)} — {sub_lib.money(price)}.\n\n"
        "Оставьте заявку — свяжемся с вами лично и подскажем, как оплатить.",
        reply_markup=renew_confirm_kb(limit, months),
    )


@router.callback_query(F.data.startswith("sub:ren:send:"))
async def renew_send(callback: CallbackQuery, bot: Bot) -> None:
    _, _, _, raw_limit, raw_months = callback.data.split(":")
    limit, months = int(raw_limit), int(raw_months)
    teacher_id = callback.from_user.id

    access = await sub_lib.access_for_teacher(teacher_id)
    # Номер нужен не для формальности: без него оплату не привязать
    # к кабинету, и заявка повиснет в воздухе
    if not access.phone:
        await callback.answer()
        await callback.message.answer(
            "Остался один шаг: поделитесь номером телефона.\n"
            "По нему мы найдём ваш кабинет, когда придёт оплата.",
            reply_markup=share_phone_kb(),
        )
        return

    price = sub_lib.term_price(limit, months)
    await add_renewal_request(teacher_id, limit, months, price)
    await callback.answer("Заявка отправлена")

    delivered = await _notify_admins(bot, teacher_id, limit, months, price)

    # Обещать «свяжемся», когда заявку никто не получил, — враньё.
    # Заявка сохранена в базе, но человеку нужен путь, который работает
    # прямо сейчас
    if delivered:
        tail = (
            f"\n\nЕсли срочно — напишите {config.SUPPORT_CONTACT}."
            if config.SUPPORT_CONTACT else ""
        )
        promise = "Свяжемся с вами в ближайшее время. Доступ включим сразу после оплаты."
    elif config.SUPPORT_CONTACT:
        promise = f"Напишите {config.SUPPORT_CONTACT} — подскажем, как оплатить."
        tail = ""
    else:
        promise = "Мы получили её и свяжемся с вами."
        tail = ""
        logger.error(
            "Заявка от %s никому не ушла: ADMIN_IDS пуст. Заявка сохранена в базе.",
            teacher_id,
        )

    await callback.message.answer(
        "✅ Заявка отправлена\n\n"
        f"{sub_lib.tariff_label(limit).capitalize()}, "
        f"{months} {sub_lib.months_word(months)} — {sub_lib.money(price)}.\n\n"
        + promise + tail
    )


async def _notify_admins(
    bot: Bot, teacher_id: int, limit: int, months: int, price: int
) -> bool:
    """Заявка падает тому, кто может её закрыть, — в этот же бот.

    Возвращает, дошла ли она хоть до кого-то: от этого зависит, что мы
    обещаем человеку в ответ.
    """
    user = await get_user(teacher_id)
    access = await sub_lib.access_for_teacher(teacher_id)
    students = await count_teacher_students(teacher_id)

    state = {
        sub_lib.KIND_TRIAL: f"пробный до {ru_date(access.until)}",
        sub_lib.KIND_PAID: f"оплачено до {ru_date(access.until)}",
    }.get(access.kind, f"доступ кончился {ru_date(access.until)}")

    text = (
        "💰 Заявка на продление\n\n"
        f"{person(user)}\n"
        f"{sub_lib.format_phone(access.phone)}\n"
        f"{sub_lib.tariff_label(limit).capitalize()} · "
        f"{months} {sub_lib.months_word(months)} · {sub_lib.money(price)}\n\n"
        f"Сейчас: {state}, учеников {students}"
    )
    markup = request_card_kb(
        teacher_id, limit, months, user.username if user else ""
    )

    delivered = False
    for admin_id in config.ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text, reply_markup=markup)
            delivered = True
        except Exception:  # noqa: BLE001
            logger.warning("Не удалось отправить заявку админу %s", admin_id)
    return delivered


# ---------- Экран заявок у админа ----------
#
# Заявка приходит сообщением, а сообщение тонет в переписке. Поэтому тот же
# разбор по одной карточке, что и у спорных вопросов: список всегда под рукой,
# и заявка уходит из него либо выдачей, либо явным закрытием.

def _humanize(value) -> str:
    """«сегодня в 14:20» / «вчера» / дата — как это читает человек."""
    if not value:
        return "—"
    days = (sub_lib._utcnow() - value).days
    if days <= 0:
        return "сегодня"
    if days == 1:
        return "вчера"
    if days < 7:
        return f"{days} дн. назад"
    return value.strftime("%d.%m.%Y")


async def _request_card(position: int) -> tuple[str, object]:
    """Карточка заявки по её месту в списке открытых."""
    requests = await list_open_renewal_requests()
    if not requests:
        return "💰 Заявок нет — все разобраны.", None

    position = max(0, min(position, len(requests) - 1))
    item = requests[position]

    user = await get_user(item["tg_user_id"])
    access = await sub_lib.access_for_teacher(item["tg_user_id"])
    students = await count_teacher_students(item["tg_user_id"])

    state = {
        sub_lib.KIND_TRIAL: f"пробный до {ru_date(access.until)}",
        sub_lib.KIND_PAID: f"оплачено до {ru_date(access.until)}",
    }.get(access.kind, f"доступ кончился {ru_date(access.until)}")

    text = (
        f"💰 Заявки — {len(requests)} "
        f"{'открытая' if len(requests) == 1 else 'открытых'}\n\n"
        f"{person(user)}\n"
        f"{sub_lib.format_phone(access.phone) if access.phone else 'номера нет'}\n"
        f"{sub_lib.tariff_label(item['student_limit']).capitalize()} · "
        f"{item['months']} {sub_lib.months_word(item['months'])} · "
        f"{sub_lib.money(item['price'])}\n"
        f"Оставлена {_humanize(_parse_iso(item['created_at']))}\n\n"
        f"Сейчас: {state}, учеников {students}"
    )
    markup = request_card_kb(
        item["tg_user_id"],
        item["student_limit"],
        item["months"],
        user.username if user else "",
        request_id=item["id"],
        position=position,
        total=len(requests),
    )
    return text, markup


def _parse_iso(value):
    try:
        return dt.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


@router.message(Command("requests"))
async def requests_cmd(message: Message) -> None:
    if not access_mod.is_admin(message.from_user.id):
        await message.answer("Нет доступа.")
        return
    text, markup = await _request_card(0)
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("sub:req:at:"))
async def request_at(callback: CallbackQuery) -> None:
    if not access_mod.is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.answer()
    position = int(callback.data.rsplit(":", 1)[-1])
    text, markup = await _request_card(position)
    await _replace(callback.message, text, markup)


@router.callback_query(F.data.startswith("sub:req:close:"))
async def request_close(callback: CallbackQuery) -> None:
    """Заявка снимается без выдачи: договорились иначе или человек передумал."""
    if not access_mod.is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    request_id = int(callback.data.rsplit(":", 1)[-1])
    closed = await close_renewal_request(request_id)
    await callback.answer("Заявка закрыта" if closed else "Она уже закрыта")

    text, markup = await _request_card(0)
    await _replace(callback.message, text, markup)


async def _replace(message: Message, text: str, markup=None) -> None:
    """Заменяет сообщение на месте — как в остальных инлайн-экранах бота."""
    try:
        await message.edit_text(text, reply_markup=markup)
    except Exception:  # noqa: BLE001
        await message.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("sub:req:ok:"))
async def request_grant(callback: CallbackQuery, bot: Bot) -> None:
    """Выдача прямо из заявки: то же, что /grant, но без набора номера."""
    if not access_mod.is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    _, _, _, raw_id, raw_limit, raw_months = callback.data.split(":")
    teacher_id, limit, months = int(raw_id), int(raw_limit), int(raw_months)

    access = await sub_lib.access_for_teacher(teacher_id)
    if not access.phone:
        await callback.answer("У преподавателя нет номера.", show_alert=True)
        return

    user = await get_user(teacher_id)
    result = await sub_lib.grant_by_phone(access.phone, months, limit)
    await callback.answer("Доступ выдан")

    # Карточку заменяем распиской прямо на месте: так видно, что с этой
    # заявкой уже разобрались, и по ней нельзя нажать «выдать» второй раз
    left = len(await list_open_renewal_requests())
    tail = f"\n\nОсталось заявок: {left} — /requests" if left else ""
    await _replace(
        callback.message,
        f"✅ Выдано: {sub_lib.tariff_label(limit)}, "
        f"{months} {sub_lib.months_word(months)}.\n"
        f"{person(user)} · доступ до {ru_date(result.access.until)}." + tail,
    )
    try:
        await bot.send_message(
            teacher_id,
            f"✅ Доступ продлён до {ru_date(result.access.until)}.\n"
            f"Учеников по тарифу: до {limit}.",
        )
    except Exception:  # noqa: BLE001
        logger.warning("Не удалось уведомить преподавателя %s", teacher_id)


@router.callback_query(F.data == "sub:open")
async def subscription_open(callback: CallbackQuery) -> None:
    """Экран подписки по кнопке из отказа заслонки."""
    await callback.answer()
    user = await get_user(callback.from_user.id)
    if not user or user.role != ROLE_TEACHER:
        return
    await sub_lib.ensure_trial(user.telegram_id)
    await show_subscription(callback.message, user.telegram_id)


@router.callback_query(F.data == "sub:share")
async def subscription_share(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.answer(
        "Нажмите кнопку внизу — телеграм передаст ваш номер.\n"
        "Он нужен, чтобы оплата нашла именно ваш кабинет.",
        reply_markup=share_phone_kb(),
    )


@router.message(F.text == BTN_CANCEL_SHARE)
async def subscription_share_cancel(message: Message) -> None:
    subject = None
    user = await get_user(message.from_user.id)
    if user:
        subject = user.current_subject
    # Возвращаем кабинет: без этого преподаватель остаётся с клавиатурой
    # из одной кнопки и без выхода
    await message.answer("Хорошо, номер не привязан.", reply_markup=teacher_cabinet_kb(subject))


@router.message(F.contact)
async def subscription_contact(message: Message) -> None:
    user = await get_user(message.from_user.id)
    contact = message.contact

    if not user or user.role != ROLE_TEACHER:
        await message.answer("Номер нужен только преподавателям — у тебя всё и так работает.")
        return

    # Чужую визитку из адресной книги не принимаем: доступ привязывается
    # к человеку, а не к тому, чей контакт он переслал
    if contact.user_id != message.from_user.id:
        await message.answer(
            "Это чужой номер. Нажмите кнопку «Отправить мой номер» — "
            "телеграм передаст ваш собственный.",
            reply_markup=share_phone_kb(),
        )
        return

    try:
        phone, access = await sub_lib.attach_phone(
            message.from_user.id, contact.phone_number
        )
    except sub_lib.PhoneTaken:
        # Клавиатуру возвращаем обязательно: иначе человек остаётся
        # с одной кнопкой «отправить номер» и без выхода
        await message.answer(
            "Этот номер уже привязан к другому кабинету.\n"
            "Если это ваш номер — напишите нам, разберёмся."
            + (f" {config.SUPPORT_CONTACT}" if config.SUPPORT_CONTACT else ""),
            reply_markup=teacher_cabinet_kb(user.current_subject),
        )
        return

    logger.info("Phone attached: teacher=%s", message.from_user.id)

    await message.answer(
        f"✅ Номер привязан: {sub_lib.format_phone(phone)}\n"
        "Теперь оплата подтянется к вашему кабинету сама.",
        reply_markup=teacher_cabinet_kb(user.current_subject),
    )
    if access.kind == sub_lib.KIND_PAID:
        # Человек оплатил до того, как зашёл в бота: доступ встал прямо сейчас
        await message.answer(
            f"И сразу хорошая новость: доступ оплачен до {ru_date(access.until)}, "
            f"учеников до {access.student_limit}."
        )
    await show_subscription(message, message.from_user.id)


# ---------- Ручная выдача ----------

GRANT_HELP = (
    "Выдача доступа:\n"
    "<code>/grant номер месяцы [лимит учеников]</code>\n\n"
    "Например: <code>/grant +375291234567 6 30</code>\n"
    f"Лимит по умолчанию — {sub_lib.DEFAULT_GRANT_LIMIT}.\n\n"
    "Незнакомый номер — не ошибка: доступ запишется впрок и встанет, "
    "когда преподаватель поделится этим номером."
)


@router.message(Command("grant"))
async def grant_cmd(message: Message, command: CommandObject, bot: Bot) -> None:
    if not access_mod.is_admin(message.from_user.id):
        await message.answer("Нет доступа.")
        return

    parts = (command.args or "").split()
    if len(parts) < 2:
        await message.answer(GRANT_HELP, parse_mode="HTML")
        return

    raw_phone, raw_months = parts[0], parts[1]
    if not raw_months.isdigit() or not 1 <= int(raw_months) <= 36:
        await message.answer("Месяцы — число от 1 до 36.")
        return
    months = int(raw_months)

    limit = sub_lib.DEFAULT_GRANT_LIMIT
    if len(parts) > 2:
        if not parts[2].isdigit() or int(parts[2]) < 1:
            await message.answer("Лимит учеников — целое число больше нуля.")
            return
        limit = int(parts[2])

    phone = sub_lib.normalize_phone(raw_phone)
    if len(phone) < 9:
        await message.answer("Не похоже на номер телефона.")
        return

    result = await sub_lib.grant_by_phone(phone, months, limit)

    if not result.applied:
        await message.answer(
            f"⏳ Такого номера в базе пока нет.\n"
            f"Доступ записан: {months} мес., до {limit} учеников.\n"
            f"Номер: {sub_lib.format_phone(result.phone)}\n\n"
            "Встанет сам, как только преподаватель поделится этим номером."
        )
        return

    teacher = await get_user(result.teacher_id)
    await message.answer(
        f"✅ Доступ выдан\n"
        f"{person(teacher)} · {sub_lib.format_phone(result.phone)}\n"
        f"До {ru_date(result.access.until)} · до {limit} учеников"
    )

    # Преподаватель должен узнать об этом от бота, а не от нас в переписке
    try:
        await bot.send_message(
            result.teacher_id,
            f"✅ Доступ продлён до {ru_date(result.access.until)}.\n"
            f"Учеников по тарифу: до {limit}.",
        )
    except Exception:  # noqa: BLE001
        logger.warning("Не удалось уведомить преподавателя %s", result.teacher_id)


# ---------- Репетиция состояний ----------

# Заслонка пропускает `sub:` всегда — поэтому из состояния «доступ кончился»
# кнопки этого экрана продолжают работать и можно вернуться обратно.
_TEST_STATES = {
    "trial30": ("🟡 Пробный — 30 дней", 30, None, sub_lib.TRIAL_STUDENT_LIMIT),
    "trial2": ("🟠 Пробный — 2 дня", 2, None, sub_lib.TRIAL_STUDENT_LIMIT),
    "expired": ("🔴 Доступ кончился", -1, None, sub_lib.TRIAL_STUDENT_LIMIT),
    "paid6": ("🟢 Оплачено — 6 месяцев", -1, 6, sub_lib.DEFAULT_GRANT_LIMIT),
}


async def _test_screen(user_id: int) -> str:
    access = await sub_lib.access_for_teacher(user_id)
    state = {
        sub_lib.KIND_TRIAL: "🟡 пробный период",
        sub_lib.KIND_PAID: "🟢 оплачено",
        sub_lib.KIND_EXPIRED: "🔴 доступ кончился",
    }.get(access.kind, "⚪️ записи о доступе нет")

    guard = (
        "🔒 заслонка проверяет и вас"
        if not access_mod.admin_bypasses(user_id)
        else "🔓 вы админ, заслонка вас не трогает"
    )

    return (
        "🧪 Репетиция состояний\n\n"
        f"Сейчас: {state}"
        + (f", до {ru_date(access.until)}" if access.until else "")
        + f"\nПроверка: {guard}\n\n"
        "Кнопки меняют состояние вашей собственной подписки — "
        "на других это не влияет."
    )


@router.message(Command("sub_test"))
async def sub_test_cmd(message: Message) -> None:
    if not access_mod.is_admin(message.from_user.id):
        await message.answer("Нет доступа.")
        return
    await sub_lib.ensure_trial(message.from_user.id)
    await message.answer(
        await _test_screen(message.from_user.id),
        reply_markup=sub_test_kb(not access_mod.admin_bypasses(message.from_user.id)),
    )


@router.callback_query(F.data.startswith("sub:test:"))
async def sub_test_action(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    if not access_mod.is_admin(user_id):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    action = callback.data.rsplit(":", 1)[-1]
    await sub_lib.ensure_trial(user_id)

    if action == "guard":
        now_guarded = access_mod.toggle_admin_guard(user_id)
        await callback.answer(
            "Теперь заслонка проверяет и вас." if now_guarded
            else "Обход вернулся: вас снова не проверяют."
        )
    elif action in _TEST_STATES:
        label, trial_days, paid_months, limit = _TEST_STATES[action]
        now = sub_lib._utcnow()
        trial_until = now + dt.timedelta(days=trial_days)
        paid_until = sub_lib.add_months(now, paid_months) if paid_months else None
        await set_subscription_dates(user_id, trial_until, paid_until, limit)
        await callback.answer(label)
    elif action == "screen":
        await callback.answer()
        await show_subscription(callback.message, user_id)
        return
    elif action == "student":
        await callback.answer()
        await callback.message.answer(
            "Что видит ученик, когда у преподавателя кончился доступ:\n\n"
            f"— — —\n{access_mod.STUDENT_BLOCKED}\n— — —\n\n"
            "А вот что в этот момент видит сам преподаватель:\n\n"
            f"— — —\n{access_mod.teacher_blocked(dt.datetime.now())}\n— — —"
        )
        return
    else:
        await callback.answer()
        return

    try:
        await callback.message.edit_text(
            await _test_screen(user_id),
            reply_markup=sub_test_kb(not access_mod.admin_bypasses(user_id)),
        )
    except Exception:  # noqa: BLE001
        # Текст мог не измениться — телеграм на это ругается, а нам всё равно
        pass


@router.message(Command("subs"))
async def subs_cmd(message: Message) -> None:
    """Кто есть кто. Без этого выдача впрок с опечаткой пропадает молча."""
    if not access_mod.is_admin(message.from_user.id):
        await message.answer("Нет доступа.")
        return

    subs = await list_subscriptions()
    pending = await list_pending_grants()
    requests = await list_open_renewal_requests()

    if not subs and not pending and not requests:
        await message.answer("Подписок пока нет.")
        return

    lines: List[str] = []

    # Заявки первыми: это единственная строка списка, где кто-то ждёт ответа
    if requests:
        lines.append(f"💰 Заявки, ждут ответа — {len(requests)}")
        for item in requests:
            user = await get_user(item["tg_user_id"])
            sub = await get_subscription(item["tg_user_id"])
            phone = sub_lib.format_phone(sub.phone) if sub and sub.phone else "номера нет"
            lines.append(
                f"   {person(user)} · {phone}\n"
                f"   до {item['student_limit']} учеников · "
                f"{item['months']} {sub_lib.months_word(item['months'])} · "
                f"{sub_lib.money(item['price'])}"
            )
        lines.append("")

    lines.append(f"💳 Подписки — {len(subs)}")
    lines.append("")
    for sub in subs:
        access = sub_lib.access_of(sub)
        mark = {
            sub_lib.KIND_PAID: "🟢",
            sub_lib.KIND_TRIAL: "🟡",
        }.get(access.kind, "🔴")
        user = await get_user(sub.tg_user_id)
        students = await count_teacher_students(sub.tg_user_id)
        phone = sub_lib.format_phone(sub.phone) if sub.phone else "номера нет"
        lines.append(
            f"{mark} {person(user)} · {phone}\n"
            f"   до {ru_date(access.until)} · учеников {students} из "
            f"{access.student_limit or sub_lib.TRIAL_STUDENT_LIMIT}"
        )

    if pending:
        lines.append("")
        lines.append(f"⏳ Выдано впрок — {len(pending)}")
        for item in pending:
            lines.append(
                f"   {sub_lib.format_phone(item['phone'])} · "
                f"{item['months']} мес. · до {item['student_limit']} учеников"
            )

    for chunk in split_message(lines):
        await message.answer(chunk)
