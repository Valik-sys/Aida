"""Разбор билетов из .docx с выгрузкой в Google Sheets.

Ядро разбора живёт в services/ticket_parser.py — здесь только CLI и запись
в таблицу. Бот использует то же ядро напрямую, минуя Google Sheets.

    python scripts/parse_tests.py <файл-или-папка> [--print-only]
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Iterable, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import gspread

import config
from services.ticket_parser import COLS, parse_docx


def _iter_docx_files(path: Path) -> Iterable[Path]:
    if path.is_file():
        yield path
        return
    yield from sorted(path.glob("*.docx"))


def _get_existing_variants(ws: gspread.Worksheet) -> set:
    """Имена вариантов, уже загруженных в таблицу."""
    try:
        col_values = ws.col_values(1)
        return {v.strip() for v in col_values[1:] if v.strip()}
    except Exception:  # noqa: BLE001
        return set()


def _open_worksheet(gc: gspread.Client) -> gspread.Worksheet:
    ss = gc.open_by_key(config.TESTS_SHEET_ID)
    if config.TESTS_WORKSHEET_NAME:
        return ss.worksheet(config.TESTS_WORKSHEET_NAME)
    return ss.sheet1


def upload_rows_to_google_sheets(rows: List[List[str]]) -> None:
    if not config.TESTS_SHEET_ID:
        raise RuntimeError("TESTS_SHEET_ID не задан.")
    if not config.GOOGLE_CREDENTIALS_PATH or not os.path.exists(config.GOOGLE_CREDENTIALS_PATH):
        raise RuntimeError(f"credentials.json не найдено: {config.GOOGLE_CREDENTIALS_PATH}")

    gc = gspread.service_account(filename=config.GOOGLE_CREDENTIALS_PATH)
    ws = _open_worksheet(gc)

    if not ws.row_values(1):
        ws.append_row(COLS)

    batch_size = 50
    for i in range(0, len(rows), batch_size):
        chunk = rows[i: i + batch_size]
        if chunk:
            ws.append_rows(chunk, value_input_option="RAW")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("path", help="Путь к docx-файлу или папке с docx-файлами")
    ap.add_argument("--print-only", action="store_true", help="Только показать, без загрузки")
    args = ap.parse_args()

    existing_variants: set = set()
    if not args.print_only:
        try:
            gc = gspread.service_account(filename=config.GOOGLE_CREDENTIALS_PATH)
            existing_variants = _get_existing_variants(_open_worksheet(gc))
            if existing_variants:
                print(f"Уже в таблице: {len(existing_variants)} вариантов")
        except Exception as e:  # noqa: BLE001
            print(f"Не удалось прочитать таблицу: {e}")

    all_rows: List[List[str]] = []
    for docx_path in _iter_docx_files(Path(args.path)):
        if docx_path.stem in existing_variants:
            print(f"Пропуск (уже в таблице): {docx_path.name}")
            continue

        result = parse_docx(docx_path)
        print(
            f"{docx_path.name}: источник={result.source} "
            f"принято={result.accepted_count} отбраковано={result.rejected_count}"
        )
        for reason, n in result.reasons_summary().items():
            print(f"    {reason}: {n}")

        all_rows.extend([str(row[col]) for col in COLS] for row in result.rows)

    if args.print_only:
        for r in all_rows[:10]:
            print(r)
        print(f"... всего строк: {len(all_rows)}")
        return

    upload_rows_to_google_sheets(all_rows)
    print(f"Загружено строк: {len(all_rows)}")


if __name__ == "__main__":
    main()
