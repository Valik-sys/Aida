from __future__ import annotations

import logging
import os
from typing import Any, List, Optional, Tuple

from langchain_community.retrievers import BM25Retriever
from langchain.retrievers.ensemble import EnsembleRetriever
from langchain_core.documents import Document
from langchain_openai import OpenAIEmbeddings
from langchain_community.vectorstores import FAISS

import config


logger = logging.getLogger(__name__)

_retriever_cache: Optional[Any] = None
_vectorstore_cache: Optional[Any] = None
_bm25_cache: Optional[Any] = None

FAISS_INDEX_FILE = "index.faiss"


def _looks_like_faiss_store(persist_dir: str) -> bool:
    return os.path.isfile(os.path.join(persist_dir, FAISS_INDEX_FILE))


def get_hybrid_retriever() -> Optional[Any]:
    global _retriever_cache  # noqa: PLW0603
    if _retriever_cache is not None:
        return _retriever_cache

    if not _looks_like_faiss_store(config.VECTORSTORE_PATH):
        logger.warning("Vectorstore not found: %s", config.VECTORSTORE_PATH)
        return None

    try:
        global _vectorstore_cache  # noqa: PLW0603
        embeddings = OpenAIEmbeddings(model=config.EMBEDDING_MODEL)
        vectorstore = FAISS.load_local(
            config.VECTORSTORE_PATH,
            embeddings,
            allow_dangerous_deserialization=True,
        )
        _vectorstore_cache = vectorstore

        # Достаём все документы для BM25
        all_docs = list(vectorstore.docstore._dict.values())
        if not all_docs:
            return None

        documents = [doc.page_content for doc in all_docs]
        metadatas = [doc.metadata for doc in all_docs]

        global _bm25_cache  # noqa: PLW0603
        vector_retriever = vectorstore.as_retriever(search_kwargs={"k": config.RAG_TOP_K})
        bm25_retriever = BM25Retriever.from_texts(documents, metadatas=metadatas, k=config.RAG_TOP_K)
        _bm25_cache = bm25_retriever

        _retriever_cache = EnsembleRetriever(
            retrievers=[vector_retriever, bm25_retriever],
            weights=[0.7, 0.3],
        )
        return _retriever_cache
    except Exception:  # noqa: BLE001
        logger.exception("Не удалось загрузить hybrid retriever")
        return None


def faiss_similarity_search(text: str, k: int = 10) -> List[Document]:
    """Direct FAISS search by arbitrary text — used for HyDE."""
    if _vectorstore_cache is None:
        return []
    try:
        return _vectorstore_cache.similarity_search(text, k=k)
    except Exception:  # noqa: BLE001
        logger.exception("FAISS similarity search error")
        return []


def bm25_search(text: str, k: int = 10) -> List[Document]:
    """Direct BM25 keyword search by arbitrary text — used for HyDE."""
    if _bm25_cache is None:
        return []
    try:
        _bm25_cache.k = k
        return _bm25_cache.invoke(text)
    except Exception:  # noqa: BLE001
        logger.exception("BM25 search error")
        return []


def build_context_from_docs(docs: List[Any], max_docs: int = 5) -> Tuple[str, List[dict]]:
    parts: List[str] = []
    sources: List[dict] = []
    for doc in docs[:max_docs]:
        meta = getattr(doc, "metadata", {}) or {}
        sources.append(meta)
        snippet = (getattr(doc, "page_content", "") or "").strip()
        if snippet:
            parts.append(f"[{meta.get('source', meta.get('chunk_title', 'chunk'))}] {snippet}")
    return "\n\n".join(parts).strip(), sources


def rerank_docs(query: str, docs: List[Any], top_n: Optional[int] = None) -> List[Any]:
    """Rerank disabled — returns docs in original order."""
    if top_n is not None:
        return docs[:top_n]
    return docs


def reset_retriever_cache() -> None:
    """Drop cached retriever so next call to get_hybrid_retriever reloads."""
    global _retriever_cache, _vectorstore_cache, _bm25_cache  # noqa: PLW0603
    _retriever_cache = None
    _vectorstore_cache = None
    _bm25_cache = None
