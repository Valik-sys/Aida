"""
Скрипт для визуальной проверки форматирования всех вопросов.

Прогоняет каждый вопрос из Google Sheets через тот же пайплайн форматирования,
что использует бот (_format_question), и генерирует HTML-отчёт для проверки.

Использование:
    python scripts/preview_questions.py
    python scripts/preview_questions.py --out data/preview.html
    # python scripts/preview_questions.py --type B        # только часть Б
    python scripts/preview_questions.py --search "соответств"  # фильтр по тексту
"""
import argparse
import asyncio
import html
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))

from services.sheets import sheets_cache
from bot.handlers.tests import (
    _format_question,
    _is_real_option,
    _is_usable_question,
    _is_usable_question_text,
    _normalize_latin_markers,
    _clean_question_text,
)

HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>Предпросмотр вопросов</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: #f0f2f5; margin: 0; padding: 20px; }}
  h1 {{ color: #333; }}
  .stats {{ background: #fff; padding: 12px 16px; border-radius: 8px;
            margin-bottom: 20px; font-size: 14px; color: #555; }}
  .filter-bar {{ margin-bottom: 16px; }}
  .filter-bar input {{ padding: 8px 12px; width: 300px; border: 1px solid #ccc;
                       border-radius: 6px; font-size: 14px; }}
  .card {{ background: #fff; border-radius: 12px; padding: 16px 20px;
           margin-bottom: 12px; box-shadow: 0 1px 3px rgba(0,0,0,.12); }}
  .card-header {{ font-size: 12px; color: #999; margin-bottom: 8px;
                  display: flex; gap: 10px; align-items: center; }}
  .badge {{ display: inline-block; padding: 2px 8px; border-radius: 10px;
            font-size: 11px; font-weight: 600; }}
  .badge-a {{ background: #d1ecf1; color: #0c5460; }}
  .badge-b {{ background: #d4edda; color: #155724; }}
  .badge-corr {{ background: #fff3cd; color: #856404; }}
  .tg-msg {{ background: #e3f2fd; border-radius: 10px; padding: 14px 16px;
             white-space: pre-wrap; font-size: 15px; line-height: 1.6;
             font-family: 'Segoe UI', sans-serif; max-width: 480px;
             color: #1a1a1a; }}
  .raw {{ margin-top: 10px; background: #f8f9fa; border-radius: 6px;
          padding: 10px 14px; white-space: pre-wrap; font-size: 12px;
          color: #666; font-family: monospace; display: none; }}
  .toggle {{ font-size: 11px; color: #888; cursor: pointer; margin-top: 6px;
             text-decoration: underline; }}
  .row-num {{ color: #bbb; font-size: 11px; }}
  .search-miss {{ display: none; }}
</style>
</head>
<body>
<h1>Предпросмотр вопросов ({total})</h1>
<div class="stats">
  Часть А: {count_a} &nbsp;|&nbsp; Часть Б: {count_b}
  &nbsp;|&nbsp; Соответствие: {count_corr}
</div>
<div class="filter-bar">
  <input type="text" id="search" placeholder="Поиск по тексту вопроса..." oninput="filterCards()">
</div>
{cards}
<script>
function filterCards() {{
  var q = document.getElementById('search').value.toLowerCase();
  document.querySelectorAll('.card').forEach(function(c) {{
    var t = c.getAttribute('data-text').toLowerCase();
    c.style.display = (!q || t.includes(q)) ? '' : 'none';
  }});
}}
function toggleRaw(btn) {{
  var raw = btn.previousElementSibling;
  if (raw.style.display === 'block') {{
    raw.style.display = 'none'; btn.textContent = 'показать исходник';
  }} else {{
    raw.style.display = 'block'; btn.textContent = 'скрыть исходник';
  }}
}}
</script>
</body>
</html>
"""

CARD_TEMPLATE = """\
<div class="card" data-text="{data_text}">
  <div class="card-header">
    <span class="row-num">строка {sheet_row}</span>
    <span class="badge {badge_cls}">{badge_label}</span>
    {extra_badge}
  </div>
  <div class="tg-msg">{formatted_html}</div>
  <div class="raw">{raw_html}</div>
  <span class="toggle" onclick="toggleRaw(this)">показать исходник</span>
</div>"""


def _build_q_dict(row: dict) -> dict:
    question_text = row.get("Вопрос") or ""
    expected_raw = (row.get("Ответ") or "").strip()
    options = {i: (row.get(f"Вар.{i}") or "").strip() for i in range(1, 6)}
    has_options = any(_is_real_option(options.get(i, "")) for i in range(1, 6))
    return {
        "type": "A" if has_options else "B",
        "expected": expected_raw,
        "question_text": question_text,
        "options": options if has_options else {},
    }


def _is_correspondence(q: dict) -> bool:
    text = (q.get("question_text") or "").lower()
    return "соответств" in text or "соотнес" in text


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="data/preview_questions.html")
    # Временный фильтр для отдельного прогона части A/Б оставлен закомментированным:
    # parser.add_argument("--type", choices=["A", "B"], help="Фильтр: только часть А или Б")
    parser.add_argument("--search", default="", help="Фильтр по тексту вопроса")
    args = parser.parse_args()

    print("Загрузка данных из Google Sheets...")
    await sheets_cache.load_all()
    rows = sheets_cache.tests_rows
    print(f"Загружено строк: {len(rows)}")

    cards_html = []
    count_a = count_b = count_corr = 0

    for i, row in enumerate(rows):
        q = _build_q_dict(row)
        question_text = q["question_text"].strip()
        if not question_text or not _is_usable_question_text(question_text) or not _is_usable_question(q):
            continue

        # Если снова понадобится отдельный прогон только части A/Б, вернуть:
        # if args.type and q["type"] != args.type:
        #     continue
        if args.search and args.search.lower() not in question_text.lower():
            continue

        total_placeholder = len(rows)
        formatted = _format_question(q, i + 1, total_placeholder)

        is_corr = _is_correspondence(q)
        if q["type"] == "A":
            count_a += 1
            badge_cls = "badge-a"
            badge_label = "Часть А"
        else:
            count_b += 1
            badge_cls = "badge-b"
            badge_label = "Часть Б"

        extra_badge = ""
        if is_corr:
            count_corr += 1
            extra_badge = '<span class="badge badge-corr">соответствие</span>'

        cards_html.append(CARD_TEMPLATE.format(
            sheet_row=i + 2,
            badge_cls=badge_cls,
            badge_label=badge_label,
            extra_badge=extra_badge,
            formatted_html=html.escape(formatted),
            raw_html=html.escape(question_text),
            data_text=html.escape(question_text.replace('"', "&quot;")),
        ))

    total = len(cards_html)
    full_html = HTML_TEMPLATE.format(
        total=total,
        count_a=count_a,
        count_b=count_b,
        count_corr=count_corr,
        cards="\n".join(cards_html),
    )

    out_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), args.out)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(full_html)

    print(f"\nГотово! Вопросов в отчёте: {total}")
    print(f"  Часть А: {count_a}, Часть Б: {count_b}, Соответствие: {count_corr}")
    print(f"Файл: {out_path}")
    print("Открой его в браузере для просмотра.")


if __name__ == "__main__":
    asyncio.run(main())
