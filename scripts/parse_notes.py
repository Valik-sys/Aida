"""
Загружает .docx файлы из data/raw/notes/ в Google Doc как markdown-текст
(## заголовки, **жирный**, - списки) для последующей индексации RAG.

Использование:
    python scripts/parse_notes.py

.env:
    NOTES_DOC_ID   — ID Google-документа для конспектов
    NOTES_TAB_ID   — (опционально) ID вкладки
    GOOGLE_CREDENTIALS_PATH — путь к JSON-ключу сервисного аккаунта
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

_project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_project_root))

from dotenv import load_dotenv
load_dotenv(_project_root / ".env")

from docx import Document as DocxDocument
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build as build_service

import config

SCOPES = ["https://www.googleapis.com/auth/documents"]
NOTES_DIR = _project_root / "data" / "raw" / "notes"


def _get_docs_service():
    creds = Credentials.from_service_account_file(
        config.GOOGLE_CREDENTIALS_PATH, scopes=SCOPES,
    )
    return build_service("docs", "v1", credentials=creds, cache_discovery=False)


def _is_major_heading(text: str) -> bool:
    """Только крупные разделы: 'Основные положения', 'Хронология', 'Выводы' и т.п."""
    t = text.lower().strip()
    keywords = [
        "основные положения", "основные выводы", "основные понятия",
        "хронология", "исторические личности", "причины", "последствия",
        "термины", "заключение",
    ]
    for kw in keywords:
        if kw in t:
            return True
    # Нумерованные разделы типа "1. Проекты развития"
    if re.match(r'^\d+[\.\)]\s', t):
        return True
    return False


def docx_to_markdown(path: Path) -> str:
    """Конвертирует .docx в простой текст с ## только на крупных разделах."""
    doc = DocxDocument(str(path))
    lines: list[str] = []

    for para in doc.paragraphs:
        style_name = para.style.name if para.style else ""
        non_empty = [r for r in para.runs if r.text and r.text.strip()]

        if not non_empty:
            lines.append("")
            continue

        text = "".join(r.text for r in para.runs).strip()

        # Docx Heading style → ## только если крупный раздел
        if style_name.startswith("Heading"):
            if _is_major_heading(text):
                lines.append(f"## {text}")
            else:
                lines.append(text)
        # Весь абзац жирный → ## только если крупный раздел
        elif all(r.bold for r in non_empty):
            if _is_major_heading(text):
                lines.append(f"## {text}")
            else:
                lines.append(text)
        elif style_name == "List Paragraph":
            lines.append(f"- {text}")
        else:
            lines.append(text)

    # Убираем лишние пустые строки подряд
    result: list[str] = []
    prev_empty = False
    for line in lines:
        if not line:
            if not prev_empty:
                result.append("")
            prev_empty = True
        else:
            result.append(line)
            prev_empty = False
    return "\n".join(result).strip()


def _get_tab_end_index(service, doc_id: str, tab_id: str) -> int:
    doc = service.documents().get(
        documentId=doc_id, includeTabsContent=True,
    ).execute()
    for tab in doc.get("tabs", []):
        props = tab.get("tabProperties", {})
        if tab_id and props.get("tabId") != tab_id:
            continue
        body = tab.get("documentTab", {}).get("body", {})
        content = body.get("content", [])
        if content:
            return content[-1].get("endIndex", 1) - 1
        break
    return 1


def _clear_doc(service, doc_id: str, tab_id: str) -> None:
    """Удаляет всё содержимое документа/вкладки (кроме финального \n)."""
    end = _get_tab_end_index(service, doc_id, tab_id)
    if end <= 1:
        return
    rng: dict = {"startIndex": 1, "endIndex": end}
    if tab_id:
        rng["tabId"] = tab_id
    service.documents().batchUpdate(
        documentId=doc_id,
        body={"requests": [{"deleteContentRange": {"range": rng}}]},
    ).execute()
    print("Документ очищен.")


def upload_notes() -> int:
    docx_files = sorted(
        f for f in NOTES_DIR.glob("*.docx") if not f.name.startswith("~")
    )
    if not docx_files:
        print(f"Нет .docx файлов в {NOTES_DIR}")
        return 0

    doc_id = config.NOTES_DOC_ID
    tab_id = config.NOTES_TAB_ID
    if not doc_id:
        print("NOTES_DOC_ID не задан в .env")
        return 0

    service = _get_docs_service()

    # Очищаем документ перед загрузкой
    print("Очистка документа...")
    _clear_doc(service, doc_id, tab_id)

    uploaded = 0

    for i, path in enumerate(docx_files):
        print(f"[{i+1}/{len(docx_files)}] {path.name} ... ", end="", flush=True)

        md = docx_to_markdown(path)
        if not md:
            print("пусто, пропуск")
            continue

        # Заголовок — имя файла
        title = path.stem.replace("_", " ")
        content = f"\n# {title}\n\n{md}\n"

        end_index = _get_tab_end_index(service, doc_id, tab_id)

        loc: dict = {"index": end_index}
        if tab_id:
            loc["tabId"] = tab_id

        service.documents().batchUpdate(
            documentId=doc_id,
            body={"requests": [{"insertText": {"location": loc, "text": content}}]},
        ).execute()

        uploaded += 1
        print("OK")

    print(f"\nЗагружено {uploaded} файлов в Google Doc.")
    return uploaded


if __name__ == "__main__":
    upload_notes()
