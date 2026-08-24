from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest
from openai import AsyncOpenAI

import config
from database.db import get_inactive_users, update_last_active

logger = logging.getLogger(__name__)

_REMINDER_CHECK_INTERVAL = 24 * 3600  # раз в сутки
_INACTIVE_DAYS = 7


async def _generate_reminder(name: str, class_name: str) -> str:
    client = AsyncOpenAI(api_key=config.OPENAI_API_KEY)
    resp = await client.chat.completions.create(
        model=config.LLM_MODEL_MINI,
        messages=[
            {
                "role": "system",
                "content": (
                    "Ты помощник по подготовке к ЦТ по истории Беларуси. "
                    "Напиши короткое живое сообщение (2–3 предложения) ученику, который не заходил неделю. "
                    "Обратись по имени. Используй ЦТ как триггер — напомни, что время идёт, "
                    "экзамен приближается, и каждая тренировка на счету. "
                    "Пиши тепло и по-дружески, с лёгкой мотивацией — без занудства. "
                    "Запрещено: «скучал». Без заголовков и списков."
                ),
            },
            {
                "role": "user",
                "content": f"Имя: {name}. Класс: {class_name}.",
            },
        ],
        temperature=0.95,
        max_tokens=120,
    )
    return resp.choices[0].message.content.strip()


async def reminder_loop(bot: Bot) -> None:
    """Background task: daily check for inactive users, sends LLM-generated nudge."""
    await asyncio.sleep(60)  # дать боту время полностью запуститься
    while True:
        try:
            users = await get_inactive_users(days=_INACTIVE_DAYS)
            logger.info("Reminder check: %d inactive users found", len(users))
            for user in users:
                try:
                    text = await _generate_reminder(user.name, user.class_name)
                    await bot.send_message(user.telegram_id, text)
                    # Сбрасываем таймер, чтобы не слать повторно раньше следующей недели
                    await update_last_active(user.telegram_id)
                    logger.info("Reminder sent to user %d (%s)", user.telegram_id, user.name)
                except (TelegramForbiddenError, TelegramBadRequest):
                    # Пользователь заблокировал бота — просто пропускаем
                    logger.warning("Cannot send reminder to %d — bot blocked or chat not found", user.telegram_id)
                except Exception:
                    logger.exception("Failed to send reminder to user %d", user.telegram_id)
                await asyncio.sleep(1)  # пауза между сообщениями, чтоб не словить rate limit
        except Exception:
            logger.exception("Reminder loop iteration error")

        await asyncio.sleep(_REMINDER_CHECK_INTERVAL)
