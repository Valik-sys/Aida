import asyncio
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import TelegramObject
from dotenv import load_dotenv

# Загружаем .env из корня проекта (рядом с config.py)
_project_root = Path(__file__).resolve().parent.parent
load_dotenv(_project_root / ".env")

import config
from bot.handlers.start import router as start_router
from bot.handlers.menu import router as menu_router, update_router
from bot.handlers.tests import router as tests_router
from bot.handlers.flashcards import router as flashcards_router
from bot.handlers.chat import router as chat_router
from bot.handlers.teacher_upload import router as teacher_upload_router
from bot.handlers.topics import router as topics_router
from bot.reminders import reminder_loop
from rag.indexer import build_vectorstore
from rag.retriever import _looks_like_faiss_store
from services.sheets import sheets_cache
from services.theory import load_theory, load_theory_from_disk
from database.db import init_db, prune_answer_log, update_last_active

logger = logging.getLogger(__name__)


class ActivityMiddleware:
    """Updates last_active_at for every user who sends a message or presses a button."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user and not user.is_bot:
            try:
                await update_last_active(user.id)
            except Exception:
                pass  # не ломаем основной флоу из-за трекинга
        return await handler(event, data)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    if not config.TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN не задан (env).")

    if not config.ADMIN_IDS:
        logger.warning(
            "ADMIN_IDS пуст — админские команды (/update, /reindex) доступны всем. "
            "Укажи свой Telegram ID в .env."
        )

    await init_db()

    # Журнал ответов чистим при старте: отдельный планировщик ради этого
    # заводить незачем, а бот перезапускается достаточно часто
    removed = await prune_answer_log()
    if removed:
        logger.info("Журнал ответов: удалено старых записей — %s", removed)

    if sheets_cache.load_from_disk():
        logger.info("Sheets loaded from disk cache — Google Sheets пропущен")
    else:
        await sheets_cache.load_all()

    if load_theory_from_disk():
        logger.info("Theory loaded from disk cache — Google Doc пропущен")
    else:
        await load_theory()

    # Загружаем векторную базу: если индекс уже есть на диске — не пересоздаём
    if config.TEXTBOOK_DOC_ID:
        persist_dir = config.VECTORSTORE_PATH
        if _looks_like_faiss_store(persist_dir):
            logger.info("Vectorstore already exists on disk — skipping rebuild (use /reindex to force)")
        else:
            try:
                n = await asyncio.to_thread(build_vectorstore)
                logger.info("Vectorstore built: %d chunks", n)
            except Exception:
                logger.exception("Не удалось построить vectorstore при старте")

    bot = Bot(token=config.TELEGRAM_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())

    dp.update.middleware(ActivityMiddleware())

    dp.include_router(start_router)
    dp.include_router(teacher_upload_router)
    dp.include_router(menu_router)
    dp.include_router(update_router)
    dp.include_router(tests_router)
    dp.include_router(flashcards_router)
    dp.include_router(chat_router)
    dp.include_router(topics_router)

    asyncio.create_task(reminder_loop(bot))

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

