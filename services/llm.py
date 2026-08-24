"""Обёртка OpenAI API для проверки ответов и чата."""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from openai import AsyncOpenAI

import config

logger = logging.getLogger(__name__)

_client: Optional[AsyncOpenAI] = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(api_key=config.OPENAI_API_KEY)
    return _client


def _get_rag_context(question: str) -> str:
    """Получает контекст из RAG (учебники) + карточки для вопроса."""
    parts: list[str] = []
    try:
        from rag.retriever import get_hybrid_retriever, build_context_from_docs

        retriever = get_hybrid_retriever()
        if retriever is not None:
            docs = retriever.invoke(question)
            if docs:
                context, _ = build_context_from_docs(docs, max_docs=5)
                if context:
                    parts.append(context)
    except Exception:
        logger.exception("RAG context retrieval error")

    try:
        from services.sheets import search_flashcards
        flash_ctx = search_flashcards(question, max_results=10)
        if flash_ctx:
            parts.append(f"Данные из карточек:\n{flash_ctx}")
    except Exception:
        logger.exception("Flashcard context retrieval error")

    return "\n\n".join(parts)


VERDICT_CORRECT = "correct"
VERDICT_WRONG = "wrong"
VERDICT_PARTIAL = "partial"
VERDICT_UNKNOWN = "unknown"


def verdict_of(response: str) -> str:
    """Что означает ответ проверки — по маркеру в начале строки.

    Живёт рядом с проверкой, потому что маркеры задаёт она. Отдельный
    «unknown» нужен для случая, когда модель недоступна и сравнить не с чем:
    такой ответ нельзя записывать в статистику ни как верный, ни как ошибку.
    """
    text = (response or "").lstrip()
    if text.startswith("✅"):
        return VERDICT_CORRECT
    if text.startswith("❌"):
        return VERDICT_WRONG
    if text.startswith("🟡"):
        return VERDICT_PARTIAL
    return VERDICT_UNKNOWN


async def check_test_answer(
    question_text: str,
    student_answer: str,
    correct_answer: str,
) -> str:
    """Проверяет ответ ученика через LLM.

    Если correct_answer есть — сравнивает с ним.
    Если correct_answer пустой — использует RAG-контекст из учебников.

    Возвращает текст ответа ученику.
    """
    client = _get_client()

    if correct_answer.strip():
        # Есть эталон в таблице
        system = (
            "Ты проверяешь ответ ученика по истории Беларуси (подготовка к ЦТ).\n"
            "Тебе дан вопрос, ответ ученика и ПРАВИЛЬНЫЙ ответ из таблицы.\n\n"
            "ПРАВИЛА:\n"
            "1. Правильный ответ из таблицы — это ЭТАЛОН. Не придумывай свой ответ.\n"
            "2. Сравни ответ ученика с эталоном по СМЫСЛУ, игнорируя порядок букв и цифр внутри пары: "
            "А1Б2В3 и 1А2Б3В — это одинаковый ответ на соответствие.\n"
            "3. Если ответ совпадает (по смыслу или номеру) — начни с ✅ и похвали кратко (1 предложение).\n"
            "4. Если не совпадает — начни с ❌, укажи правильный ответ из эталона и объясни почему "
            "он верный (2-4 предложения).\n"
            "5. Если правильных ответов НЕСКОЛЬКО (перечислены через ';'), перечисли каждый "
            "на отдельной строке с пометкой номера.\n"
            "6. Обращайся к ученику напрямую на 'ты' (например: 'Ты ответил верно!', "
            "'К сожалению, ты ошибся.').\n"
            "7. Отвечай на русском, кратко."
        )
        user = (
            f"Вопрос:\n{question_text}\n\n"
            f"Ответ ученика: {student_answer}\n"
            f"Правильный ответ: {correct_answer}"
        )
    else:
        # Эталона нет — используем RAG-контекст из учебников
        rag_context = await asyncio.to_thread(_get_rag_context, question_text)
        system = (
            "Ты проверяешь ответ ученика по истории Беларуси (подготовка к ЦТ).\n"
            "Эталонного ответа в таблице НЕТ. Используй контекст из учебников для проверки.\n\n"
            "ПРАВИЛА:\n"
            "1. Опирайся ТОЛЬКО на контекст из учебников. Не выдумывай факты.\n"
            "2. Если ответ ученика верный по контексту — начни с ✅ и похвали кратко.\n"
            "3. Если неверный — начни с ❌, дай правильный ответ из контекста и объясни.\n"
            "4. Если в контексте нет достаточной информации — скажи об этом честно.\n"
            "5. Обращайся к ученику напрямую на 'ты' (например: 'Ты ответил верно!', "
            "'К сожалению, ты ошибся.').\n"
            "6. Отвечай на русском, кратко."
        )
        context_block = f"\n\nКонтекст из учебников:\n{rag_context}" if rag_context else ""
        user = (
            f"Вопрос:\n{question_text}\n\n"
            f"Ответ ученика: {student_answer}"
            f"{context_block}"
        )

    try:
        resp = await client.chat.completions.create(
            model=config.LLM_MODEL_MINI,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.3,
            max_tokens=500,
        )
        return resp.choices[0].message.content.strip()
    except Exception:
        logger.exception("LLM check_test_answer error")
        if correct_answer.strip():
            # Простое нормализованное сравнение без LLM
            norm_s = "".join(c for c in student_answer.upper() if c.isalnum())
            norm_c = "".join(c for c in correct_answer.upper() if c.isalnum())
            if norm_s and norm_c and norm_s == norm_c:
                return "✅ Верно!"
            return f"❌ Правильный ответ: {correct_answer}"
        return "Не удалось проверить ответ. Попробуй позже."
