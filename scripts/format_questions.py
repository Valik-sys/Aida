"""
Скрипт для форматирования вопросов на соответствие/последовательность.

Использует GPT-4o-mini для ВИЗУАЛЬНОГО форматирования (только переносы строк).
Никакой текст НЕ меняется — проверка символ-в-символ после удаления пробелов.

Этапы:
  1) Загружает вопросы из Google Sheets
  2) Фильтрует только соответствие + последовательность (без реальных вариантов)
  3) Отправляет каждый в LLM: «только расставь переносы строк»
  4) Валидирует: strip whitespace == оригинал
  5) Сохраняет preview в data/formatted_preview.txt
  6) С флагом --write записывает колонку «Вопрос_форм» обратно в таблицу
"""
import argparse
import asyncio
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))

import gspread
from openai import AsyncOpenAI

import config
from services.sheets import sheets_cache

client = AsyncOpenAI(api_key=config.OPENAI_API_KEY)

SYSTEM_PROMPT = """\
Ты форматируешь текст вопроса для отображения в Telegram-боте.

ПРАВИЛА (НАРУШЕНИЕ ЛЮБОГО — КРИТИЧЕСКАЯ ОШИБКА):
1. НЕ МЕНЯЙ НИ ОДНОГО СЛОВА, НИ ОДНОЙ БУКВЫ, НИ ОДНОГО ЗНАКА ПРЕПИНАНИЯ.
2. НЕ УДАЛЯЙ и НЕ ДОБАВЛЯЙ текст. Никаких пояснений, комментариев, нумерации.
3. Единственное, что ты можешь делать — ВСТАВЛЯТЬ ПЕРЕНОСЫ СТРОК (\\n) для читаемости.

Как форматировать:
- Вопрос-заголовок — отдельная строка.
- Каждый пункт (А), Б), В), Г)) — с новой строки.
- Каждое описание/определение — с новой строки.
- Если в конце есть «Ответ:» — оставь на отдельной строке.

Верни ТОЛЬКО переформатированный текст, без кавычек и пояснений."""


def _strip_ws(s: str) -> str:
    """Убирает ВСЕ пробельные символы и невидимые Unicode-символы для побуквенного сравнения."""
    # Убираем все категории Unicode-пробелов + управляющие символы
    return re.sub(r"[\s\u00a0\u200b\u200c\u200d\u2060\ufeff]+", "", s)


def _is_real_option(text: str) -> bool:
    cleaned = re.sub(r"[\d\)\(\s,;.\-]+", "", text.strip())
    return len(cleaned) > 0


def _needs_formatting(row: dict) -> bool:
    q = (row.get("Вопрос") or "").strip().lower()
    has_opts = any(
        _is_real_option((row.get(f"Вар.{i}") or "").strip()) for i in range(1, 6)
    )
    if has_opts:
        return False
    return ("соответств" in q or "соотнес" in q or
            "последовательн" in q or "хронологическ" in q)


SEM = asyncio.Semaphore(10)  # макс 10 параллельных запросов


async def format_one(original: str) -> tuple[str, str, bool]:
    """Возвращает (llm_результат, оригинал, прошёл_проверку).
    
    Всегда возвращает LLM-текст (даже при fail) для диагностики.
    """
    async with SEM:
        try:
            resp = await client.chat.completions.create(
                model=config.LLM_MODEL,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": original},
                ],
                temperature=0,
                max_tokens=2000,
            )
            formatted = (resp.choices[0].message.content or "").strip()
        except Exception as e:
            print(f"  [ERROR] API: {e}")
            return original, original, False

    # Валидация: после удаления всех пробелов текст должен совпасть
    ok = _strip_ws(formatted) == _strip_ws(original)
    if not ok:
        so = _strip_ws(original)
        sr = _strip_ws(formatted)
        diff_pos = next((i for i in range(min(len(so), len(sr))) if so[i] != sr[i]), -1)
        print(f"  [DIFF] len orig={len(so)} res={len(sr)} first_diff_at={diff_pos}")
        if diff_pos >= 0:
            print(f"    orig: ...{so[max(0,diff_pos-15):diff_pos+15]}...")
            print(f"    res:  ...{sr[max(0,diff_pos-15):diff_pos+15]}...")
        elif len(so) != len(sr):
            print(f"    Обрезка/добавление: orig ends ...{so[-20:]}")
            print(f"                        res ends  ...{sr[-20:]}")
    return formatted, original, ok


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true",
                        help="Записать результат в Google Sheets (колонка 'Вопрос_форм')")
    args = parser.parse_args()

    print("Загрузка данных из Google Sheets...")
    await sheets_cache.load_all()
    rows = sheets_cache.tests_rows
    print(f"Загружено {len(rows)} вопросов.")

    # Определяем индексы строк, которым нужно форматирование (1-based, +1 за заголовок)
    to_format: list[tuple[int, str]] = []  # (row_index_in_sheet, original_text)
    for i, row in enumerate(rows):
        if _needs_formatting(row):
            to_format.append((i, (row.get("Вопрос") or "").strip()))

    print(f"Вопросов для форматирования: {len(to_format)}")
    if not to_format:
        print("Нечего форматировать.")
        return

    # Отправляем в LLM
    print("Отправка в GPT-4o-mini...")
    tasks = [format_one(text) for _, text in to_format]
    results = await asyncio.gather(*tasks)

    passed = sum(1 for _, _, ok in results if ok)
    failed = sum(1 for _, _, ok in results if not ok)
    print(f"Прошли валидацию: {passed}/{len(results)}")
    print(f"Отклонены (текст изменился): {failed}")

    # Сохраняем превью
    preview_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "data", "formatted_preview.txt"
    )
    with open(preview_path, "w", encoding="utf-8") as f:
        for idx_pair, (formatted, _orig, ok) in zip(to_format, results):
            sheet_idx, original = idx_pair
            status = "OK" if ok else "REJECTED"
            f.write(f"=== Строка {sheet_idx + 2} [{status}] ===\n")
            f.write(f"--- ОРИГИНАЛ ---\n{original}\n")
            f.write(f"--- РЕЗУЛЬТАТ ---\n{formatted}\n\n")
    print(f"Превью сохранено: {preview_path}")

    if not args.write:
        print("\nДля записи в таблицу запустите с --write")
        return

    # Запись в Google Sheets
    print("Запись в Google Sheets...")
    gc = gspread.service_account(filename=config.GOOGLE_CREDENTIALS_PATH)
    ss = gc.open_by_key(config.TESTS_SHEET_ID)
    ws = ss.worksheet(config.TESTS_WORKSHEET_NAME) if config.TESTS_WORKSHEET_NAME else ss.sheet1

    # Найти или создать колонку «Вопрос_форм»
    headers = ws.row_values(1)
    col_name = "Вопрос_форм"
    if col_name in headers:
        col_idx = headers.index(col_name) + 1
    else:
        col_idx = len(headers) + 1
        ws.update_cell(1, col_idx, col_name)
        print(f"Создана колонка '{col_name}' (столбец {col_idx})")

    # Пакетная запись
    cells_to_update = []
    for (sheet_row_idx, _original), (formatted, _orig, ok) in zip(to_format, results):
        if ok:
            row_in_sheet = sheet_row_idx + 2  # +1 за заголовок, +1 за 1-based
            cells_to_update.append(gspread.Cell(row_in_sheet, col_idx, formatted))

    if cells_to_update:
        ws.update_cells(cells_to_update, value_input_option="RAW")
        print(f"Записано {len(cells_to_update)} ячеек в колонку '{col_name}'.")
    else:
        print("Нет ячеек для записи.")

    print("Готово!")


if __name__ == "__main__":
    asyncio.run(main())
