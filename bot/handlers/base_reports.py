"""Разбор жалоб на общие вопросы базы — у админа, а не у преподавателя.

Общий вопрос скрывается у всех сразу, когда на брак пожаловались ученики
хотя бы двух разных преподавателей (`reports.BASE_HIDE_TEACHERS`). Решение
«вернуть или убрать навсегда» принимает один человек: иначе один
преподаватель возвращал бы вопрос, другой скрывал, и никто не знал бы,
в каком он состоянии.

Карточка приходит сама в момент скрытия, а постоянный список — по команде
`/base_reports`: сообщение тонет в переписке, а скрытый вопрос ждёт решения.
"""

from __future__ import annotations

import logging
from typing import List, Optional

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import config
from bot import access as access_mod
from bot.keyboards.inline import short_text
from database.db import (
    get_base_question_status,
    get_reports_for_question,
    list_base_questions_by_status,
    question_hash,
    set_base_question_status,
)
from services import reports
from services.sheets import sheets_cache


logger = logging.getLogger(__name__)

router = Router()

PREFIX = "base"

# Длинный вопрос режем: карточке нужен текст, чтобы судить, а не весь абзац
QUESTION_LIMIT = 700


def _find_base_row(qhash: str) -> Optional[dict]:
    for row in sheets_cache.base_tests_rows:
        if question_hash(row.get("Вопрос") or "") == qhash:
            return row
    return None


def _reasons_lines(summary: dict) -> List[str]:
    lines = []
    for reason, count in sorted(summary["reasons"].items(), key=lambda p: -p[1]):
        label = reports.REASON_SHORT.get(reason, reason)
        lines.append(f"• {label} — {count}")
    return lines


def _teachers_word(n: int) -> str:
    return "преподавателя" if n % 10 == 1 and n % 100 != 11 else "преподавателей"


async def card_text(qhash: str, header: str) -> str:
    """Вопрос целиком с вариантами — без них судить о браке не о чем."""
    row = _find_base_row(qhash)
    summary = await get_reports_for_question(qhash)

    lines = [header, ""]
    if row:
        question = (row.get("Вопрос") or "").strip()
        if len(question) > QUESTION_LIMIT:
            question = question[:QUESTION_LIMIT].rstrip() + "…"
        lines.append(question)
        options = [
            f"{i}) {(row.get(f'Вар.{i}') or '').strip()}"
            for i in range(1, 6)
            if (row.get(f"Вар.{i}") or "").strip()
        ]
        if options:
            lines.append("")
            lines.extend(options)
        answer = (row.get("Ответ") or "").strip()
        lines.append("")
        lines.append(f"Ответ в базе: {answer or 'не указан'}")
    else:
        # Вопроса нет в базе — его могли исправить в таблице и перечитать.
        # Жалоба при этом осталась, показываем то, что сохранилось
        lines.append(f"«{short_text(summary['preview'] or '')}»")
        lines.append("")
        lines.append("Этого вопроса в базе уже нет — возможно, его исправили.")

    lines.append("")
    lines.append(
        f"Жалобы: {summary['total']} — от учеников {summary['teachers']} "
        f"{_teachers_word(summary['teachers'])}"
    )
    lines.extend(_reasons_lines(summary))
    return "\n".join(lines)


def card_kb(
    qhash: str, position: Optional[int] = None, total: Optional[int] = None
) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="↩️ Вернуть в базу", callback_data=f"{PREFIX}:ok:{qhash}")],
        [InlineKeyboardButton(text="🗑 Убрать навсегда", callback_data=f"{PREFIX}:del:{qhash}")],
    ]
    if position is not None and total and total > 1:
        rows.append([
            InlineKeyboardButton(text="◀️", callback_data=f"{PREFIX}:at:{(position - 1) % total}"),
            InlineKeyboardButton(text=f"{position + 1} из {total}", callback_data="noop"),
            InlineKeyboardButton(text="▶️", callback_data=f"{PREFIX}:at:{(position + 1) % total}"),
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def notify_admins_hidden(bot: Bot, qhash: str) -> None:
    """Карточка в момент скрытия. Молчать нельзя: вопрос пропал у всех."""
    text = await card_text(qhash, "⚠️ Общий вопрос скрыт у всех")
    for admin_id in config.ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text, reply_markup=card_kb(qhash))
        except Exception:  # noqa: BLE001
            logger.warning("Не удалось отправить карточку общего вопроса админу %s", admin_id)


async def _screen(position: int) -> tuple[str, Optional[InlineKeyboardMarkup]]:
    hidden = await list_base_questions_by_status(reports.STATUS_HIDDEN)
    if not hidden:
        return "✅ Скрытых общих вопросов нет — всё разобрано.", None
    position = max(0, min(position, len(hidden) - 1))
    qhash = hidden[position]
    text = await card_text(qhash, f"⚠️ Общие вопросы на разборе — {len(hidden)}")
    return text, card_kb(qhash, position, len(hidden))


async def _replace(message: Message, text: str, markup=None) -> None:
    try:
        await message.edit_text(text, reply_markup=markup)
    except Exception:  # noqa: BLE001
        await message.answer(text, reply_markup=markup)


@router.message(Command("base_reports"))
async def base_reports_cmd(message: Message) -> None:
    if not access_mod.is_admin(message.from_user.id):
        await message.answer("Нет доступа.")
        return
    text, markup = await _screen(0)
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith(f"{PREFIX}:at:"))
async def base_reports_at(callback: CallbackQuery) -> None:
    if not access_mod.is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.answer()
    text, markup = await _screen(int(callback.data.rsplit(":", 1)[-1]))
    await _replace(callback.message, text, markup)


@router.callback_query(F.data.regexp(rf"^{PREFIX}:(ok|del):"))
async def base_reports_decide(callback: CallbackQuery) -> None:
    """Решение по общему вопросу — сразу у всех преподавателей.

    «Вернуть» ставит подтверждение: такой вопрос по жалобам больше
    не скрывается, иначе его убирали бы снова и снова.
    """
    if not access_mod.is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    _, action, qhash = callback.data.split(":", 2)
    status = await get_base_question_status(qhash)
    if status in (reports.STATUS_CONFIRMED, reports.STATUS_REMOVED):
        # Кнопка из старого сообщения: решение уже принято, расписка —
        # по тому, что стоит в базе, а не по нажатой кнопке
        await callback.answer("По этому вопросу уже решено")
    elif action == "ok":
        status = reports.STATUS_CONFIRMED
        await set_base_question_status(qhash, status)
        await callback.answer("Вопрос вернулся в базу")
    else:
        status = reports.STATUS_REMOVED
        await set_base_question_status(qhash, status)
        await callback.answer("Вопрос убран навсегда")

    left = len(await list_base_questions_by_status(reports.STATUS_HIDDEN))
    done = (
        "↩️ Вернулся в базу" if status == reports.STATUS_CONFIRMED
        else "🗑 Убран навсегда"
    )
    tail = f"\n\nОсталось на разборе: {left} — /base_reports" if left else ""
    row = _find_base_row(qhash)
    preview = short_text((row or {}).get("Вопрос") or "") if row else "вопрос"
    # Карточку заменяем распиской: по старой кнопке второй раз не решить
    await _replace(callback.message, f"{done}\n«{preview}»{tail}")
