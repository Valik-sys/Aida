from __future__ import annotations

import asyncio
import logging
from itertools import zip_longest
from typing import Any, Dict, Optional

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from tenacity import retry, stop_after_attempt, wait_fixed

import config
from rag.prompts import (
    CHAT_SYSTEM_PROMPT,
    CHAT_USER_PROMPT,
    EXPLAIN_ERROR_SYSTEM_PROMPT,
    EXPLAIN_ERROR_USER_PROMPT,
)
from rag.retriever import bm25_search, build_context_from_docs, faiss_similarity_search, get_hybrid_retriever, rerank_docs


logger = logging.getLogger(__name__)
NO_INFO_TEXT = "В учебных материалах этой информации нет."
_NO_INFO_MARKERS = ("в учебных материалах этой информации нет", "в материалах нет", "нет информации")


def _make_llm() -> ChatOpenAI:
    return ChatOpenAI(model=config.LLM_MODEL, temperature=0.2)


@retry(stop=stop_after_attempt(2), wait=wait_fixed(1))
async def _call_llm(system_prompt: str, user_prompt: str) -> str:
    llm = _make_llm()
    messages = [SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)]
    resp = await llm.ainvoke(messages)
    return (resp.content or "").strip()


@retry(stop=stop_after_attempt(2), wait=wait_fixed(1))
async def _call_llm_with_history(system_prompt: str, history: list, user_prompt: str) -> str:
    from langchain_core.messages import AIMessage
    llm = _make_llm()
    messages = [SystemMessage(content=system_prompt)]
    for msg in history:
        if msg["role"] == "user":
            messages.append(HumanMessage(content=msg["content"]))
        else:
            messages.append(AIMessage(content=msg["content"]))
    messages.append(HumanMessage(content=user_prompt))
    resp = await llm.ainvoke(messages)
    return (resp.content or "").strip()


async def _hyde_search(question: str) -> str:
    """HyDE: generate hypothetical answer → embed it → search FAISS → return context."""
    try:
        llm = ChatOpenAI(model=config.LLM_MODEL_MINI, temperature=0.5)
        messages = [
            SystemMessage(content=(
                "Ты эксперт по истории Беларуси (школьный курс, ЦТ). "
                "Напиши короткий фактический ответ на вопрос: имена, даты, конкретные факты. "
                "ВАЖНО: термины в вопросе могут быть военными кодовыми названиями, "
                "историческими понятиями или названиями операций — не трактуй их в бытовом смысле. "
                "Например, «Витебские ворота» — это разрыв в линии фронта, а не городские ворота. "
                "Это используется только для поиска по базе знаний, не как финальный ответ."
            )),
            HumanMessage(content=question),
        ]
        resp = await llm.ainvoke(messages)
        hypothetical = (resp.content or "").strip()
        logger.info("HyDE hypothetical: %s", hypothetical[:200])
    except Exception:  # noqa: BLE001
        logger.exception("HyDE generation error")
        return ""

    # FAISS по гипотетическому ответу
    faiss_docs = await asyncio.to_thread(faiss_similarity_search, hypothetical, 8)

    # BM25 по гипотетическому ответу — находит чанки с конкретными именами/датами
    bm25_docs = await asyncio.to_thread(bm25_search, hypothetical, 8)
    logger.info("HyDE BM25 sources: %s", [d.metadata.get('source') for d in bm25_docs])

    # Чередуем FAISS и BM25 чтобы оба попадали в контекст (не конкатенация — иначе BM25 вытесняется)
    seen: set[str] = set()
    all_docs: list[Any] = []
    for faiss_doc, bm25_doc in zip_longest(faiss_docs, bm25_docs):
        for doc in (faiss_doc, bm25_doc):
            if doc is None:
                continue
            content = (getattr(doc, "page_content", "") or "").strip()
            if content and content not in seen:
                seen.add(content)
                all_docs.append(doc)

    if not all_docs:
        return ""

    all_docs = await asyncio.to_thread(rerank_docs, question, all_docs)
    context, _ = build_context_from_docs(all_docs, max_docs=8)
    logger.info("HyDE context sources: %s", [d.metadata.get('source') for d in all_docs[:8]])
    return context


async def explain_error(question: str, correct_answer: str) -> Optional[Dict[str, Any]]:
    retriever = get_hybrid_retriever()
    if retriever is None:
        return None

    query = f"Вопрос ученика:\n{question}\n\nПравильный ответ:\n{correct_answer}"
    try:
        docs = await asyncio.to_thread(retriever.invoke, query)
    except Exception:  # noqa: BLE001
        logger.exception("RAG retrieval error")
        return None

    if not docs:
        return None

    docs = await asyncio.to_thread(rerank_docs, query, docs)
    context, sources = build_context_from_docs(docs, max_docs=5)
    if not context:
        return None

    user_prompt = EXPLAIN_ERROR_USER_PROMPT.format(
        question=question,
        correct_answer=correct_answer,
        context=context,
    )
    try:
        text = await _call_llm(EXPLAIN_ERROR_SYSTEM_PROMPT, user_prompt)
    except Exception:  # noqa: BLE001
        logger.exception("LLM explain error")
        return None

    return {"text": text, "sources": sources}


async def _contextualize_query(question: str, history: list) -> str:
    """nano-LLM превращает вопрос + история в самостоятельный поисковый запрос."""
    try:
        llm = ChatOpenAI(model=config.LLM_MODEL_MINI, temperature=0)
        history_text = "\n".join(
            f"{'Ученик' if m['role'] == 'user' else 'Бот'}: {m['content']}"
            for m in history[-6:]
        )
        messages = [
            SystemMessage(content=(
                "Перепиши последний вопрос ученика как самостоятельный поисковый запрос "
                "для поиска по базе знаний по истории Беларуси. "
                "Используй конкретные термины и факты из контекста диалога. "
                "Верни только запрос — одну строку, без пояснений."
            )),
            HumanMessage(content=f"История диалога:\n{history_text}\n\nПоследний вопрос: {question}"),
        ]
        resp = await llm.ainvoke(messages)
        result = (resp.content or "").strip().splitlines()[0].strip()
        logger.info("Contextualized retrieval query: %s", result[:120])
        return result or question
    except Exception:  # noqa: BLE001
        logger.exception("Query contextualization error")
        return question


async def chat_answer(question: str, history: list | None = None) -> str:
    retriever = get_hybrid_retriever()
    q = (question or "").strip()
    if not q:
        return NO_INFO_TEXT

    retrieval_q = await _contextualize_query(q, history) if history else q

    # RAG context from textbooks
    rag_context = ""
    if retriever is not None:
        try:
            docs = await asyncio.to_thread(retriever.invoke, retrieval_q)
            if docs:
                docs = await asyncio.to_thread(rerank_docs, q, docs)
                rag_context, _sources = build_context_from_docs(docs, max_docs=5)
        except Exception:  # noqa: BLE001
            logger.exception("RAG retrieval error")

    # Flashcard context
    flash_context = ""
    try:
        from services.sheets import search_flashcards
        flash_context = await asyncio.to_thread(search_flashcards, retrieval_q, 10)
    except Exception:  # noqa: BLE001
        logger.exception("Flashcard context error")

    context_parts = [p for p in [rag_context, f"Данные из карточек:\n{flash_context}" if flash_context else ""] if p]
    context = "\n\n".join(context_parts)

    if not context:
        return NO_INFO_TEXT

    logger.info("=== CONTEXT SENT TO LLM ===\n%s\n=== END CONTEXT ===", context)

    user_prompt = CHAT_USER_PROMPT.format(question=q, context=context)
    try:
        if history:
            text = await _call_llm_with_history(CHAT_SYSTEM_PROMPT, history, user_prompt)
        else:
            text = await _call_llm(CHAT_SYSTEM_PROMPT, user_prompt)
    except Exception:  # noqa: BLE001
        logger.exception("LLM chat error")
        return NO_INFO_TEXT

    def _is_no_info(t: str) -> bool:
        return any(m in (t or "").lower() for m in _NO_INFO_MARKERS)

    best_text = text

    # Уровень 2: HyDE — гипотетический ответ как поисковый запрос
    if _is_no_info(text):
        logger.info("Still no-info, trying HyDE...")
        hyde_context = await _hyde_search(retrieval_q)
        if hyde_context and hyde_context != context:
            logger.info("HyDE search succeeded")
            hyde_prompt = CHAT_USER_PROMPT.format(question=q, context=hyde_context)
            try:
                if history:
                    candidate = await _call_llm_with_history(CHAT_SYSTEM_PROMPT, history, hyde_prompt)
                else:
                    candidate = await _call_llm(CHAT_SYSTEM_PROMPT, hyde_prompt)
                if not _is_no_info(candidate):
                    best_text = candidate
                text = candidate
            except Exception:  # noqa: BLE001
                logger.exception("LLM HyDE chat error")

    # Если HyDE дал только "нет" — вернуть лучший частичный ответ с уровня 1
    if _is_no_info(text) and not _is_no_info(best_text):
        logger.info("HyDE gave no-info, falling back to best partial answer")
        text = best_text

    return text or NO_INFO_TEXT

