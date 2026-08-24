from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Dict, List, Optional

import config
from services import storage
from subjects import DEFAULT_SUBJECT

_CACHE_PATH = storage.base_theory_path(DEFAULT_SUBJECT)

logger = logging.getLogger(__name__)

PAGE_SIZE = 1750

_theory_cache: Dict[str, str] = {}  # tab_title -> text


def _load_sync() -> Dict[str, str]:
    from rag.indexer import fetch_all_tabs
    tabs = fetch_all_tabs(config.THEORY_DOC_ID)
    return {tab["title"]: tab["text"] for tab in tabs}


async def load_theory() -> int:
    global _theory_cache  # noqa: PLW0603
    if not config.THEORY_DOC_ID:
        logger.info("THEORY_DOC_ID не задан — теория отключена")
        return 0
    try:
        result = await asyncio.to_thread(_load_sync)
        _theory_cache = result
        logger.info("Theory cache loaded: %d tabs", len(result))
        _save_theory_to_disk(result)
        return len(result)
    except Exception:  # noqa: BLE001
        logger.exception("Не удалось загрузить теорию из Google Doc")
        return 0


def _save_theory_to_disk(cache: Dict[str, str]) -> None:
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        logger.info("Theory cache saved to disk: %s", _CACHE_PATH)
    except Exception:
        logger.exception("Не удалось сохранить theory cache на диск")


def load_theory_from_disk() -> bool:
    """Загружает теорию с диска в память. Возвращает True если успешно."""
    global _theory_cache  # noqa: PLW0603
    if not _CACHE_PATH.exists():
        return False
    try:
        data = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
        _theory_cache = data
        logger.info("Theory cache loaded from disk: %d tabs", len(data))
        return True
    except Exception:
        logger.exception("Не удалось прочитать theory cache с диска")
        return False


def _format_paragraphs(text: str) -> str:
    """Каждый абзац на отдельной строке с пустой строкой между ними."""
    text = text.replace("\n", "\n\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def get_theory_pages(topic_name: str) -> List[str]:
    """Возвращает список страниц для темы, или [] если тема не найдена."""
    text = _find_tab(topic_name)
    if not text:
        return []
    text = _format_paragraphs(text)
    pages: List[str] = []
    start = 0
    while start < len(text):
        end = start + PAGE_SIZE
        if end < len(text):
            nl = text.rfind("\n", start, end)
            if nl > start:
                end = nl
        page = text[start:end].strip()
        if page:
            pages.append(page)
        start = end
    return pages


def _normalize_title(s: str) -> str:
    return s.strip().rstrip(".").strip().lower()


def _find_tab(topic_name: str) -> Optional[str]:
    if not _theory_cache:
        return None
    # Точное совпадение
    if topic_name in _theory_cache:
        return _theory_cache[topic_name]
    # Нормализованное совпадение (убирает точки, lowercase)
    norm = _normalize_title(topic_name)
    for title, text in _theory_cache.items():
        if _normalize_title(title) == norm:
            return text
    # Вкладки часто называются "1_2 Название темы." — ищем по префиксу кода
    prefix = topic_name + " "
    for title, text in _theory_cache.items():
        if title.startswith(prefix):
            return text
    # Объединённые темы: код "2_3_4" → ищем вкладку "2_3 2_4" или "2_3 2_4 Название"
    parts = topic_name.split("_")
    if len(parts) == 3:
        sec, t1, t2 = parts
        alt = f"{sec}_{t1} {sec}_{t2}"
        if alt in _theory_cache:
            return _theory_cache[alt]
        alt_norm = _normalize_title(alt)
        for title, text in _theory_cache.items():
            if _normalize_title(title) == alt_norm or title.startswith(alt + " "):
                return text
    return None
