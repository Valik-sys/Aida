from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import subjects as subjects_cfg
from bot.keyboards.inline import main_menu_kb, role_kb, subjects_kb
from bot.states.states import Onboarding, Register
from database.db import (
    bind_student,
    ensure_teacher,
    ensure_user,
    get_teacher_by_invite,
    get_user,
    set_profile,
    set_role,
    set_subject,
)
from database.models import ROLE_STUDENT, ROLE_TEACHER, Teacher
from services import storage


logger = logging.getLogger(__name__)

router = Router()


async def invite_link(bot: Bot, code: str) -> str:
    """Ссылка-приглашение вида t.me/<бот>?start=<код>."""
    me = await bot.get_me()
    return f"https://t.me/{me.username}?start={code}"


def _menu_text(user_name: str, subject: str, role: str | None) -> str:
    name = subjects_cfg.subject_name(subject)
    if role == ROLE_TEACHER:
        return f"Предмет: {name}\n\nВыбирайте, что делать:"
    greeting = f"{user_name}, " if user_name else ""
    return f"{greeting}предмет: {name}\n\nВыбирай режим:"


async def _show_menu(message: Message, user) -> None:
    subject = user.current_subject or subjects_cfg.DEFAULT_SUBJECT
    await message.answer(
        _menu_text(user.name, subject, user.role),
        reply_markup=main_menu_kb(subject, user.role),
    )


async def _attach_to_teacher(message: Message, state: FSMContext, teacher: Teacher) -> None:
    """Привязывает ученика к преподавателю и ведёт дальше по анкете."""
    student_id = message.from_user.id
    await bind_student(student_id, teacher.tg_user_id, teacher.subject)

    subject_name = subjects_cfg.subject_name(teacher.subject)
    user = await get_user(student_id)

    if user and user.name:
        # Уже знакомы — анкету повторно не спрашиваем
        await state.clear()
        await message.answer(f"Готово! Теперь ты занимаешься по предмету «{subject_name}».")
        await _show_menu(message, await get_user(student_id))
        return

    await state.clear()
    await message.answer(
        f"Готово! Предмет: {subject_name}.\n\nКак тебя зовут?"
    )
    await state.set_state(Register.waiting_name)


@router.message(Command("start"))
async def start_cmd(message: Message, command: CommandObject, state: FSMContext) -> None:
    await state.clear()
    telegram_id = message.from_user.id
    user = await ensure_user(telegram_id)

    # Переход по ссылке-приглашению: код лежит в параметре start
    code = (command.args or "").strip() if command else ""
    if code:
        teacher = await get_teacher_by_invite(code)
        if not teacher:
            await message.answer(
                "Такое приглашение не найдено — возможно, ссылка устарела.\n"
                "Попроси у преподавателя новую."
            )
        elif teacher.tg_user_id == telegram_id:
            await message.answer("Это ваша собственная ссылка — по ней присоединяются ученики.")
        elif user.role == ROLE_TEACHER:
            # Иначе преподаватель, открывший чужую ссылку, молча становится
            # учеником и теряет свой кабинет без пути назад
            await message.answer(
                "Вы зарегистрированы как преподаватель, поэтому присоединиться "
                "к другому преподавателю нельзя.\n"
                "Ваш кабинет остался на месте."
            )
        else:
            logger.info("Student %s joined teacher %s by link", telegram_id, teacher.tg_user_id)
            await _attach_to_teacher(message, state, teacher)
            return

    if user.role and user.current_subject:
        if user.role == ROLE_TEACHER:
            await message.answer("С возвращением! 👋")
        else:
            await message.answer(f"С возвращением, {user.name or 'друг'}! 💪")
        await _show_menu(message, user)
        return

    await message.answer(
        "Привет! Я AIda — помогаю преподавателям превращать их материалы "
        "в тренажёр для учеников.\n\n"
        "Скажите, кто вы:",
        reply_markup=role_kb(),
    )
    await state.set_state(Onboarding.waiting_role)


# ---------- Шаг 1: роль ----------

@router.callback_query(F.data.startswith("role:"))
async def choose_role(callback: CallbackQuery, state: FSMContext) -> None:
    role = callback.data.split(":", 1)[1]
    if role not in (ROLE_TEACHER, ROLE_STUDENT):
        await callback.answer("Неизвестная роль")
        return

    await ensure_user(callback.from_user.id)
    await set_role(callback.from_user.id, role)
    await callback.answer()

    if role == ROLE_TEACHER:
        await callback.message.answer(
            "Отлично! Какой предмет вы ведёте?", reply_markup=subjects_kb()
        )
        await state.set_state(Onboarding.waiting_subject)
        return

    # Ученику предмет не выбирают — он достаётся от преподавателя вместе с кодом
    await callback.message.answer(
        "Отлично! Введи код приглашения от своего преподавателя — "
        "шесть букв и цифр.\n\n"
        "Если преподаватель прислал ссылку, просто открой её."
    )
    await state.set_state(Onboarding.waiting_invite)


# ---------- Шаг 2а: предмет (преподаватель) ----------

@router.callback_query(F.data.startswith("subject:"))
async def choose_subject(callback: CallbackQuery, state: FSMContext) -> None:
    subject = callback.data.split(":", 1)[1]
    if subject not in subjects_cfg.all_subject_keys():
        await callback.answer("Неизвестный предмет")
        return

    telegram_id = callback.from_user.id
    await set_subject(telegram_id, subject)
    user = await get_user(telegram_id)
    await callback.answer()

    if subjects_cfg.is_stub(subject):
        await callback.message.answer(
            f"{subjects_cfg.subject_name(subject)} пока в разработке — "
            f"доступен только режим «{subjects_cfg.MODE_LABELS['ask']}»."
        )

    if not (user and user.role == ROLE_TEACHER):
        await state.clear()
        await _show_menu(callback.message, user)
        return

    teacher = await ensure_teacher(telegram_id, subject)
    storage.ensure_teacher_dirs(telegram_id, subject)
    logger.info("Teacher registered: id=%s subject=%s", telegram_id, subject)

    link = await invite_link(callback.bot, teacher.invite_code)
    await state.clear()
    await callback.message.answer(
        f"Готово! Предмет: {subjects_cfg.subject_name(subject)}.\n\n"
        f"Ссылка для учеников:\n{link}\n"
        f"Код на случай, если ссылка не открывается: <b>{teacher.invite_code}</b>\n\n"
        "Дальше загрузите свои билеты — тренажёр соберётся сам.",
        parse_mode="HTML",
    )
    await _show_menu(callback.message, user)


# ---------- Шаг 2б: код приглашения (ученик) ----------

@router.message(Onboarding.waiting_invite)
async def enter_invite(message: Message, state: FSMContext) -> None:
    code = (message.text or "").strip()

    # Ученик может вставить ссылку целиком — достаём код из неё
    if "start=" in code:
        code = code.rsplit("start=", 1)[-1].strip()

    teacher = await get_teacher_by_invite(code)
    if not teacher:
        await message.answer(
            "Не нашёл такого кода. Проверь и введи ещё раз — "
            "или попроси у преподавателя ссылку."
        )
        return

    if teacher.tg_user_id == message.from_user.id:
        await message.answer("Это ваш собственный код — по нему присоединяются ученики.")
        return

    await _attach_to_teacher(message, state, teacher)


# ---------- Шаг 3: анкета ученика ----------

@router.message(Register.waiting_name, ~F.text.startswith("/"))
async def register_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name:
        await message.answer("Напиши, пожалуйста, имя.")
        return

    # Класс не спрашиваем: на подбор материалов он не влияет — их задаёт
    # преподаватель. Поле в БД осталось для будущей статистики.
    await set_profile(message.from_user.id, name=name)
    await state.clear()

    user = await get_user(message.from_user.id)
    await message.answer(f"Отлично, {name}! 🚀 Начнём — ты справишься!")
    await _show_menu(message, user)
