from __future__ import annotations

import random
from collections import deque
from typing import Any, Deque, Dict, List

from aiogram import Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from bot.keyboards.inline import (
    flashcards_action_kb,
    flashcards_finish_kb,
    flashcards_reveal_kb,
    flashcards_section_kb,
)

_SECTION_FIELD = "Раздел"


from services import content_provider


router = Router()

flash_sessions: Dict[int, Dict[str, Any]] = {}


def _cards(category: str) -> List[Dict[str, str]]:
    """Карточки категории.

    Источник выбирает провайдер по политике предмета. Сейчас у всех предметов
    карточки общие, поэтому предмет и преподаватель ему не передаются; когда
    появится загрузка своих карточек, сюда надо будет протянуть пользователя.
    """
    return content_provider.get_flashcards_category(category)


def _get_sections(category: str) -> List[str]:
    """Возвращает отсортированный список разделов из данных категории."""
    cards = _cards(category)
    sections = sorted(
        {str(c.get(_SECTION_FIELD, "")).strip() for c in cards},
        key=lambda s: (int(s) if s.isdigit() else float("inf"), s),
    )
    return [s for s in sections if s]


def _card_front_title(card: Dict[str, str], category: str) -> tuple[str, str]:
    """Return (front_text, answer_title) based on category."""
    if category == "dates":
        return card.get("Событие", ""), card.get("Дата", "")
    elif category == "concepts":
        return card.get("Определение", ""), card.get("Понятие", "")
    else:
        return card.get("Роль в истории", ""), card.get("Личность", "")


async def _send_current_card(user_id: int, chat_id: int, bot: Any) -> None:
    session = flash_sessions.get(user_id)
    if not session:
        return

    queue: Deque[str] = session["queue"]
    if not queue:
        return

    card_id = queue[0]
    card = session["cards_by_id"].get(card_id) or {}
    category = card.get("_cat") or session["category"]
    revealed = bool(session.get("revealed"))

    front, title = _card_front_title(card, category)
    front = front[:1].upper() + front[1:] if front else front

    if revealed:
        await bot.send_message(
            chat_id=chat_id,
            text=f"{front}\n\n<b>Ответ: {title}</b>",
            parse_mode="HTML",
            reply_markup=flashcards_action_kb(card_id=card_id),
        )
    else:
        await bot.send_message(
            chat_id=chat_id,
            text=front,
            reply_markup=flashcards_reveal_kb(card_id=card_id),
        )


@router.callback_query(lambda c: c.data and c.data.startswith("flash:cat:"))  # type: ignore[call-arg]
async def flash_choose_category(callback: CallbackQuery, state: FSMContext) -> None:
    category = (callback.data or "").split(":")[-1]
    if category not in ("dates", "concepts", "persons", "mixed"):
        await callback.answer()
        return

    # Для дат — разделы всегда есть (жёстко заданы 1–8)
    if category == "dates":
        sections = _get_sections("dates")
        await state.update_data(flash_pending_category="dates")
        await callback.message.edit_text("Выбери раздел:", reply_markup=flashcards_section_kb(sections or None))
        await callback.answer()
        return

    # Для понятий и личностей — показываем разделы если они есть в таблице
    if category in ("concepts", "persons"):
        sections = _get_sections(category)
        if sections:
            await state.update_data(flash_pending_category=category)
            await callback.message.edit_text("Выбери раздел:", reply_markup=flashcards_section_kb(sections))
            await callback.answer()
            return
        else:
            # Разделы ещё не заполнены в таблице — запускаем без фильтра
            await _start_flash_session(callback, state, category=category, section=None)
            return

    await _start_flash_session(callback, state, category=category, section=None)


@router.callback_query(lambda c: c.data and c.data.startswith("flash:section:"))  # type: ignore[call-arg]
async def flash_choose_section(callback: CallbackQuery, state: FSMContext) -> None:
    section_raw = (callback.data or "").split(":")[-1]
    section = None if section_raw == "all" else section_raw

    data = await state.get_data()
    category = data.get("flash_pending_category") or "dates"
    await state.update_data(flash_pending_category=None)

    await _start_flash_session(callback, state, category=category, section=section)


async def _start_flash_session(
    callback: CallbackQuery,
    state: FSMContext,
    *,
    category: str,
    section: str | None,
) -> None:
    if category == "mixed":
        all_cards: List[Dict[str, str]] = []
        for cat in ("dates", "concepts", "persons"):
            for card in _cards(cat):
                tagged = dict(card)
                tagged["_cat"] = cat
                all_cards.append(tagged)
        cards = all_cards
    else:
        cards = _cards(category)
        if section is not None:
            cards = [c for c in cards if str(c.get("Раздел", "")).strip() == section]

    if not cards:
        await callback.message.edit_text("Пока в таблице нет карточек для этого раздела.")
        await callback.answer()
        return

    random.shuffle(cards)
    cards_by_id: Dict[str, Dict[str, str]] = {str(i): c for i, c in enumerate(cards)}
    queue: Deque[str] = deque(list(cards_by_id.keys()))

    flash_sessions[callback.from_user.id] = {
        "category": category,
        "queue": queue,
        "cards_by_id": cards_by_id,
        "revealed": False,
        "weak_ids": None,
        "total": len(cards_by_id),
    }

    category_names = {
        "dates": "Даты",
        "concepts": "Понятия",
        "persons": "Личности",
        "mixed": "Все вперемешку",
    }
    cat_label = category_names.get(category, "")
    section_label = f" — Раздел {section}" if section else ""

    await state.clear()
    await callback.message.edit_text(
        f"Карточки: {cat_label}{section_label} ({len(cards)} шт.)\n"
        "Прочитай описание и попробуй вспомнить ответ.\n"
        "Нажми \"Показать ответ\", затем отметь — знал или нет."
    )
    await _send_current_card(callback.from_user.id, callback.message.chat.id, callback.bot)
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("flash:reveal:"))  # type: ignore[call-arg]
async def flash_reveal(callback: CallbackQuery) -> None:
    parts = (callback.data or "").split(":")
    card_id = parts[-1] if parts else ""

    session = flash_sessions.get(callback.from_user.id)
    if not session or not card_id:
        await callback.answer()
        return

    queue: Deque[str] = session["queue"]
    if not queue or queue[0] != card_id:
        await callback.answer()
        return

    session["revealed"] = True

    card = session["cards_by_id"].get(card_id) or {}
    category = card.get("_cat") or session["category"]
    front, title = _card_front_title(card, category)

    await callback.message.edit_text(
        f"{front}\n\n<b>Ответ: {title}</b>",
        parse_mode="HTML",
        reply_markup=flashcards_action_kb(card_id=card_id),
    )
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("flash:mark:"))  # type: ignore[call-arg]
async def flash_mark(callback: CallbackQuery) -> None:
    session = flash_sessions.get(callback.from_user.id)
    if not session:
        await callback.answer()
        return

    parts = (callback.data or "").split(":")
    if len(parts) < 4:
        await callback.answer()
        return

    action = parts[2]  # know/unknown
    card_id = parts[-1]

    queue: Deque[str] = session["queue"]
    if not queue or queue[0] != card_id:
        await callback.answer()
        return

    if not session.get("revealed"):
        await callback.answer("Сначала нажми «Показать ответ».", show_alert=False)
        return

    if action == "know":
        queue.popleft()
    else:
        queue.popleft()
        queue.append(card_id)

    session["revealed"] = False

    # Убираем кнопки у текущей карточки, текст остаётся
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    await callback.answer()

    if not queue:
        total = session["total"]
        await callback.message.answer(
            f"Результат: из {total} карточек все отмечены как знакомые.",
            reply_markup=flashcards_finish_kb(repeat_weak_available=False),
        )
        return

    await _send_current_card(callback.from_user.id, callback.message.chat.id, callback.bot)


@router.callback_query(lambda c: c.data == "flash:finish")  # type: ignore[call-arg]
async def flash_finish(callback: CallbackQuery) -> None:
    session = flash_sessions.get(callback.from_user.id)
    if not session:
        await callback.answer()
        return

    queue: Deque[str] = session["queue"]
    total = session["total"]
    unknown = len(queue)
    known = total - unknown
    session["weak_ids"] = list(queue)

    await callback.message.answer(
        f"Результат: из {total} карточек — {known} знал, {unknown} не знал.",
        reply_markup=flashcards_finish_kb(repeat_weak_available=unknown > 0),
    )
    await callback.answer()


@router.callback_query(lambda c: c.data == "flash:repeat_weak")  # type: ignore[call-arg]
async def flash_repeat_weak(callback: CallbackQuery) -> None:
    session = flash_sessions.get(callback.from_user.id)
    if not session:
        await callback.answer()
        return

    weak_ids = session.get("weak_ids") or []
    if not weak_ids:
        await callback.answer()
        return

    session["queue"] = deque(list(weak_ids))
    session["weak_ids"] = None
    session["revealed"] = False

    await callback.message.answer("Повторяем карточки, которые не знал.")
    await _send_current_card(callback.from_user.id, callback.message.chat.id, callback.bot)
    await callback.answer()


@router.callback_query(lambda c: c.data == "menu:back_to_main")  # type: ignore[call-arg]
async def back_to_menu(callback: CallbackQuery) -> None:
    flash_sessions.pop(callback.from_user.id, None)
    await callback.answer()

