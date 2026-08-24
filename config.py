import os
from typing import List


def _env_int(name: str, default: int) -> int:
    val = os.getenv(name)
    if val is None or val.strip() == "":
        return default
    return int(val)


def _env_list_int(name: str, default: List[int]) -> List[int]:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return [int(x.strip()) for x in raw.split(",") if x.strip()]


# Telegram
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()

# OpenAI
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4.1").strip()
LLM_MODEL_MINI = os.getenv("LLM_MODEL_MINI", "gpt-4o-mini").strip()
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-3-large").strip()

# Cohere Rerank
COHERE_API_KEY = os.getenv("COHERE_API_KEY", "").strip()
RERANK_MODEL = os.getenv("RERANK_MODEL", "rerank-v3.5").strip()
RERANK_TOP_N = _env_int("RERANK_TOP_N", 8)

# Google Sheets
TESTS_SHEET_ID = os.getenv("TESTS_SHEET_ID", "").strip()
FLASHCARDS_SHEET_ID = os.getenv("FLASHCARDS_SHEET_ID", "").strip()

# Worksheet titles (optional)
TESTS_WORKSHEET_NAME = os.getenv("TESTS_WORKSHEET_NAME", "").strip()
TOPIC_TESTS_WORKSHEET_NAME = os.getenv("TOPIC_TESTS_WORKSHEET_NAME", "Вопросы по темам").strip()
FLASH_DATES_WORKSHEET_NAME = os.getenv("FLASH_DATES_WORKSHEET_NAME", "Даты").strip()
FLASH_CONCEPTS_WORKSHEET_NAME = os.getenv("FLASH_CONCEPTS_WORKSHEET_NAME", "Понятия").strip()
FLASH_PERSONS_WORKSHEET_NAME = os.getenv("FLASH_PERSONS_WORKSHEET_NAME", "Личности").strip()

# Google Doc with textbook (all tabs)
TEXTBOOK_DOC_ID = os.getenv("TEXTBOOK_DOC_ID", "").strip()

# Google Doc for notes/conspects (separate from textbook)
NOTES_DOC_ID = os.getenv("NOTES_DOC_ID", "").strip()
NOTES_TAB_ID = os.getenv("NOTES_TAB_ID", "").strip()

# Google Doc with theory per topic (one tab = one topic, tab title = topic name)
THEORY_DOC_ID = os.getenv("THEORY_DOC_ID", "").strip()

# service account key
GOOGLE_CREDENTIALS_PATH = os.getenv("GOOGLE_CREDENTIALS_PATH", "credentials.json").strip()

# RAG
VECTORSTORE_PATH = os.getenv("VECTORSTORE_PATH", "data/vectorstore/").strip()
CHROMA_COLLECTION_NAME = os.getenv("CHROMA_COLLECTION_NAME", "history_ct_belarus").strip()
RAG_TOP_K = _env_int("RAG_TOP_K", 7)

# SQLite
HISTORY_CT_DB_PATH = os.getenv("HISTORY_CT_DB_PATH", "data/history_ct.sqlite3").strip()

# Admin
ADMIN_IDS = _env_list_int("ADMIN_IDS", [])

