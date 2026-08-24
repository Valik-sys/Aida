from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from google.oauth2.service_account import Credentials
from google.auth.transport.requests import AuthorizedSession
from langchain_core.documents import Document
from langchain_openai import OpenAIEmbeddings
from langchain_community.vectorstores import FAISS
from langchain_text_splitters import RecursiveCharacterTextSplitter

import config


logger = logging.getLogger(__name__)

_heading_re = re.compile(r"^#{1,4}\s+\S")

MAX_CHUNK_CHARS = 3000  # ~750 tokens

SCOPES = ["https://www.googleapis.com/auth/documents.readonly"]


# ── Google Docs helpers ──────────────────────────────────────────


def _get_authorized_session() -> AuthorizedSession:
    creds = Credentials.from_service_account_file(
        config.GOOGLE_CREDENTIALS_PATH, scopes=SCOPES,
    )
    return AuthorizedSession(creds)


def _extract_text_from_elements(elements: list) -> str:
    """Recursively extract plain text from Google Doc structural elements."""
    parts: list[str] = []
    for el in elements:
        if "paragraph" in el:
            for run in el["paragraph"].get("elements", []):
                tc = run.get("textRun", {}).get("content", "")
                if tc:
                    parts.append(tc)
        elif "table" in el:
            rows = el["table"].get("tableRows", [])
            row_texts: list[str] = []
            for row in rows:
                cells = row.get("tableCells", [])
                cell_texts = [
                    _extract_text_from_elements(cell.get("content", [])).strip().replace("\n", " ")
                    for cell in cells
                ]
                row_texts.append(" │ ".join(cell_texts))
            if row_texts:
                # Добавляем разделитель после первой строки (шапка таблицы)
                row_texts.insert(1, "─" * 40)
            parts.append("\n" + "\n".join(row_texts) + "\n")
        elif "sectionBreak" in el:
            pass
    return "".join(parts)


def _collect_tabs(tabs: list, results: List[dict], depth: int = 0) -> None:
    """Рекурсивно обходит вкладки и дочерние вкладки."""
    for tab in tabs:
        props = tab.get("tabProperties", {})
        title = props.get("title", "Untitled")
        body = tab.get("documentTab", {}).get("body", {})
        text = _extract_text_from_elements(body.get("content", []))
        indent = "  " * depth
        logger.info("%stab '%s': %d chars", indent, title, len(text))
        if text.strip():
            results.append({"title": title, "text": text.strip()})
        child_tabs = tab.get("childTabs", [])
        if child_tabs:
            _collect_tabs(child_tabs, results, depth + 1)


def fetch_all_tabs(doc_id: str) -> List[dict]:
    """Fetch all tabs from a Google Doc (including nested). Returns list of {title, text}."""
    session = _get_authorized_session()
    url = f"https://docs.googleapis.com/v1/documents/{doc_id}"
    resp = session.get(url, params={"includeTabsContent": "true"}, timeout=120)
    resp.raise_for_status()
    doc = resp.json()

    results: List[dict] = []
    tabs = doc.get("tabs", [])
    logger.info("fetch_all_tabs: doc_id=%s top_level_tabs=%d", doc_id, len(tabs))
    _collect_tabs(tabs, results)

    # Фоллбэк: если вкладок нет — читаем основной body (старый формат документа)
    if not results:
        title = doc.get("title", "Untitled")
        body = doc.get("body", {})
        text = _extract_text_from_elements(body.get("content", []))
        logger.info("No tabs found, falling back to doc body: '%s' (%d chars)", title, len(text))
        if text.strip():
            results.append({"title": title, "text": text.strip()})

    return results


# ── Chunking ─────────────────────────────────────────────────────


def _sub_chunk(text: str, max_chars: int = MAX_CHUNK_CHARS) -> List[str]:
    if len(text) <= max_chars:
        return [text]
    paragraphs = re.split(r"\n{2,}", text)
    chunks: List[str] = []
    current: List[str] = []
    current_len = 0
    for para in paragraphs:
        if current and current_len + len(para) + 2 > max_chars:
            chunks.append("\n\n".join(current))
            current = []
            current_len = 0
        current.append(para)
        current_len += len(para) + 2
    if current:
        chunks.append("\n\n".join(current))
    return chunks


def chunk_text_by_section(text: str) -> List[str]:
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines) if _heading_re.search(line)]
    if not starts:
        return _sub_chunk(text.strip())
    starts.append(len(lines))
    chunks: List[str] = []
    for i in range(len(starts) - 1):
        section = "\n".join(lines[starts[i] : starts[i + 1]]).strip()
        if section:
            chunks.extend(_sub_chunk(section))
    return chunks


# ── Recursive splitter for textbook ──────────────────────────────

_textbook_splitter = RecursiveCharacterTextSplitter.from_tiktoken_encoder(
    encoding_name="cl100k_base",
    chunk_size=300,
    chunk_overlap=50,
    separators=["\n## ", "\n### ", "\n#### ", "\n\n", "\n", ". ", " ", ""],
)


# ── Build vectorstore ────────────────────────────────────────────


def build_documents_from_google_doc(
    doc_id: str, *, use_recursive_splitter: bool = False,
) -> List[Document]:
    tabs = fetch_all_tabs(doc_id)
    docs: List[Document] = []
    for tab in tabs:
        if use_recursive_splitter:
            chunks = _textbook_splitter.split_text(tab["text"])
        else:
            chunks = chunk_text_by_section(tab["text"])
        for idx, chunk in enumerate(chunks):
            docs.append(
                Document(
                    page_content=chunk,
                    metadata={
                        "source": tab["title"],
                        "chunk_index": idx,
                    },
                )
            )
    logger.info(
        "Google Doc fetched: tabs=%d chunks=%d", len(tabs), len(docs),
    )
    return docs


def build_vectorstore(
    doc_id: Optional[str] = None,
    persist_dir: str = "data/vectorstore/",
    retries: int = 3,
) -> int:
    """Build FAISS vectorstore from Google Doc(s). Returns chunk count."""
    doc_id = doc_id or config.TEXTBOOK_DOC_ID
    if not doc_id:
        raise RuntimeError("TEXTBOOK_DOC_ID не задан в .env")

    # Retry wrapper для сетевых сбоев
    for attempt in range(1, retries + 1):
        try:
            docs = build_documents_from_google_doc(doc_id, use_recursive_splitter=True)
            if config.NOTES_DOC_ID and config.NOTES_DOC_ID != doc_id:
                notes_docs = build_documents_from_google_doc(config.NOTES_DOC_ID)
                docs.extend(notes_docs)
            break
        except (TimeoutError, OSError, ConnectionError) as e:
            if attempt == retries:
                raise
            logger.warning("Attempt %d/%d failed: %s — retrying in 5s", attempt, retries, e)
            time.sleep(5)

    if not docs:
        raise RuntimeError("Google Doc пуст — нечего индексировать")

    embeddings = OpenAIEmbeddings(model=config.EMBEDDING_MODEL, chunk_size=50)
    os.makedirs(persist_dir, exist_ok=True)
    vectorstore = FAISS.from_documents(docs, embedding=embeddings)
    vectorstore.save_local(persist_dir)
    logger.info("Vectorstore built: docs=%s persist_dir=%s", len(docs), persist_dir)
    return len(docs)


if __name__ == "__main__":
    n = build_vectorstore()
    print(f"Готово — {n} чанков проиндексировано.")
