"""Скачивает Google Doc как текст и сохраняет в data/markdown/ для индексации."""
from __future__ import annotations

import os
import sys

import requests
from google.oauth2.service_account import Credentials


# Google Doc ID из ссылки
DOC_IDS = {
    "textbook_main": "1Xr5d-8Jjdw0kUixhQoGDoHwmdij_qdy6ZDJGp0USZHc",
}

SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/documents.readonly",
]
CREDS_PATH = os.getenv("GOOGLE_CREDENTIALS_PATH", "ai-target-485617-004c48747766.json")
OUTPUT_DIR = "data/markdown"


def main() -> None:
    if not os.path.exists(CREDS_PATH):
        print(f"Файл credentials не найден: {CREDS_PATH}")
        sys.exit(1)

    creds = Credentials.from_service_account_file(CREDS_PATH, scopes=SCOPES)
    from google.auth.transport.requests import Request
    creds.refresh(Request())

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    for name, doc_id in DOC_IDS.items():
        print(f"Скачиваю: {name} ({doc_id})...")
        url = f"https://docs.google.com/document/d/{doc_id}/export?format=txt"
        resp = requests.get(url, headers={"Authorization": f"Bearer {creds.token}"}, timeout=120)
        if resp.status_code != 200:
            print(f"  Ошибка {resp.status_code}: {resp.text[:300]}")
            continue

        text = resp.text
        out_path = os.path.join(OUTPUT_DIR, f"{name}.md")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"  Сохранено: {out_path} ({len(text)} символов)")

    print("Готово! Теперь запусти: python -m rag.indexer")


if __name__ == "__main__":
    main()
