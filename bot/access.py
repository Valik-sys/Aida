"""Проверка доступа — одной заслонкой на входе, а не в каждом режиме.

Режимов много, и в каждом свои точки входа: кнопка нижней клавиатуры,
инлайн-кнопка, команда, старое сообщение из истории чата. Расставлять
проверку по ним всем — гарантированная дырка. Поэтому проверка одна,
до хендлеров, и добавление нового режима её не касается.

По истечении доступа бот замолкает целиком: и на материалах преподавателя,
и на общих. Ученику при этом говорится «занятия временно недоступны» —
честное «преподаватель не продлил» подставляет преподавателя перед его же
клиентом. Давить на оплату надо уведомлением самому преподавателю.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram.types import CallbackQuery, Message, TelegramObject

import config
from bot.keyboards.inline import (
    BTN_CANCEL_SHARE,
    MENU_BTN_BACK_TO_CABINET,
    MENU_BTN_SUBSCRIPTION,
    blocked_kb,
)
from database.db import get_user
from database.models import ROLE_TEACHER
from services import subscription as sub_lib
from services.subscription import ru_date


logger = logging.getLogger(__name__)


# Что работает даже с истёкшим доступом: сам экран подписки, привязка номера
# и команды. Без этого продлить доступ было бы нечем.
# «В кабинет» тоже: кнопки «Подписка» на клавиатуре режима ученика нет,
# и преподаватель, которого доступ застал там, иначе остался бы в тупике.
ALLOWED_TEXTS = frozenset({
    MENU_BTN_SUBSCRIPTION, BTN_CANCEL_SHARE, MENU_BTN_BACK_TO_CABINET,
})
ALLOWED_CALLBACK_PREFIX = "sub:"

# Админы заслонку не проходят — иначе своим же аккаунтом не проверишь, что
# бот вообще работает. Но тогда и посмотреть на блокировку своими глазами
# нельзя, поэтому обход снимается по одному человеку через /sub_test.
# Держится в памяти: перезапуск возвращает всё как было, и забытый
# на сервере снятый обход сам себя чинит.
GUARD_ON_ADMINS: set[int] = set()


def is_admin(user_id: int) -> bool:
    """Строгая проверка админа — для всего, что касается денег.

    В проекте прижилась идиома `if config.ADMIN_IDS and user_id not in ...`:
    при пустом списке она пускает всех. Для `/reindex` это стоило денег
    на эмбеддингах, а для выдачи доступа означало бы, что любой желающий
    открывает себе платный тариф и читает телефоны всех преподавателей.
    Поэтому здесь наоборот: **нет списка — нет админов.**
    """
    return user_id in config.ADMIN_IDS


def admin_bypasses(user_id: int) -> bool:
    return bool(config.ADMIN_IDS) and user_id in config.ADMIN_IDS and (
        user_id not in GUARD_ON_ADMINS
    )


def toggle_admin_guard(user_id: int) -> bool:
    """Включает или снимает обход для одного админа. True — заслонка теперь его ловит."""
    if user_id in GUARD_ON_ADMINS:
        GUARD_ON_ADMINS.discard(user_id)
        return False
    GUARD_ON_ADMINS.add(user_id)
    return True

STUDENT_BLOCKED = (
    "Занятия временно недоступны — скоро всё заработает.\n"
    "Загляни попозже 👌"
)

STUDENT_BLOCKED_SHORT = "Занятия временно недоступны — скоро всё заработает."


def teacher_blocked(until) -> str:
    return (
        f"⏸ Доступ закончился {ru_date(until)}.\n\n"
        "Занятия остановлены — у вас и у ваших учеников. "
        "Материалы сохранены полностью, ничего не потеряно.\n\n"
        "Продлить — кнопкой ниже."
    )


def _passes_without_access(event: TelegramObject) -> bool:
    """Действия, которые нельзя запирать: иначе доступ не продлить."""
    if isinstance(event, Message):
        if event.contact is not None:
            return True
        text = (event.text or "").strip()
        return text.startswith("/") or text in ALLOWED_TEXTS
    if isinstance(event, CallbackQuery):
        return (event.data or "").startswith(ALLOWED_CALLBACK_PREFIX)
    return False


class SubscriptionMiddleware:
    """Пускает дальше, только пока доступ действует."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user = data.get("event_from_user")
        if tg_user is None or tg_user.is_bot:
            return await handler(event, data)

        if admin_bypasses(tg_user.id):
            return await handler(event, data)

        if _passes_without_access(event):
            return await handler(event, data)

        user = await get_user(tg_user.id)
        # Незарегистрированный идёт по онбордингу: перекрывать его нечем,
        # доступа у него ещё нет по определению
        if user is None or not user.role:
            return await handler(event, data)

        access = await sub_lib.access_for_user(user)

        # Записи о доступе нет вовсе — так выглядят все, кто зарегистрировался
        # до появления подписки. Молча пропускать их нельзя: это доступ
        # навсегда. Заводим пробный месяц с первого же действия.
        if access.kind == sub_lib.KIND_NONE:
            owner = user.telegram_id if user.role == ROLE_TEACHER else user.teacher_id
            if owner:
                await sub_lib.ensure_trial(owner)
                access = await sub_lib.access_for_user(user)

        if access.active:
            return await handler(event, data)

        # Ученик без преподавателя — это не про оплату, а про привязку:
        # ему отвечают собственные экраны, и они понятнее общей заглушки
        if access.kind == sub_lib.KIND_NONE:
            return await handler(event, data)

        await self._refuse(event, user.role == ROLE_TEACHER, access)
        return None

    async def _refuse(self, event: TelegramObject, is_teacher: bool, access) -> None:
        if isinstance(event, CallbackQuery):
            # Всплывающее окно вместо нового сообщения: нажатий может быть
            # много, и каждое не должно засорять переписку
            text = teacher_blocked(access.until) if is_teacher else STUDENT_BLOCKED_SHORT
            await event.answer(text[:200], show_alert=True)
            return
        if isinstance(event, Message):
            if is_teacher:
                # Кнопка, а не совет «откройте Подписку»: какая у человека
                # сейчас нижняя клавиатура, мы не знаем
                await event.answer(
                    teacher_blocked(access.until), reply_markup=blocked_kb()
                )
            else:
                await event.answer(STUDENT_BLOCKED)
