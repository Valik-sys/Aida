"""Чтение PDF: страницы → строки текста для парсера билетов.

Парсеру всё равно, откуда пришли строки — он разбирает список абзацев.
Поэтому поддержка PDF это не второй парсер, а только превращение файла
в такой список.

**Главное, что здесь решается: отличить настоящий PDF от скана.** Скан —
это картинки страниц, букв в файле нет. Часто поверх картинки лежит слой
от программы распознавания, и он бывает нечитаемым: вместо «Прочитайте
текст» извлекается «дяежи а ч а ожани». Молча принять такой файл нельзя —
преподаватель увидит «0 вопросов» и не поймёт, при чём тут он.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import List

import pdfplumber


logger = logging.getLogger(__name__)


class PdfError(Exception):
    """Файл не читается как PDF с текстом."""


# Сколько букв на страницу считаем признаком того, что текст в файле есть.
# У скана без слоя распознавания их ноль; у настоящей страницы билета —
# тысячи. Порог низкий намеренно: лучше пропустить сомнительный файл
# в парсер, где его встретит фильтр качества, чем отказать хорошему.
MIN_CHARS_PER_PAGE = 40

# Доля «слов», состоящих из одной-двух букв. У битого слоя распознавания
# текст рассыпан на огрызки («дяежи а ч а ожани»), у нормального текста
# таких слов меньше половины даже с учётом предлогов и союзов.
MAX_CRUMBS_SHARE = 0.55
MIN_WORDS_TO_JUDGE = 40

_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)


def is_pdf_name(filename: str) -> bool:
    return (filename or "").lower().endswith(".pdf")


def _crumbs_share(text: str) -> float:
    """Доля огрызков среди слов. 0 — если судить не по чему."""
    words = _WORD_RE.findall(text)
    if len(words) < MIN_WORDS_TO_JUDGE:
        return 0.0
    crumbs = sum(1 for w in words if len(w) <= 2)
    return crumbs / len(words)


def extract_lines(path: Path) -> List[str]:
    """Строки текста из PDF по порядку страниц.

    Пустые строки выбрасываются: парсер узнаёт вопросы по маркерам
    в начале строки, и лишние пропуски ему только мешают.
    """
    try:
        with pdfplumber.open(str(path)) as pdf:
            pages = len(pdf.pages)
            chunks = [page.extract_text() or "" for page in pdf.pages]
    except Exception as exc:  # noqa: BLE001
        raise PdfError("Файл повреждён или не является PDF") from exc

    if not pages:
        raise PdfError("В файле нет страниц")

    text = "\n".join(chunks)
    letters = sum(len(word) for word in _WORD_RE.findall(text))

    if letters < MIN_CHARS_PER_PAGE * pages:
        raise PdfError(
            "Похоже, это скан: страницы — картинки, текста в файле нет. "
            "Распознавать картинки бот пока не умеет."
        )

    share = _crumbs_share(text)
    if share > MAX_CRUMBS_SHARE:
        logger.info("pdf %s: текст рассыпан, огрызков %.0f%%", path.name, share * 100)
        raise PdfError(
            "В файле есть текстовый слой, но он нечитаемый — так бывает "
            "у сканов, обработанных программой распознавания. "
            "Нужен PDF с настоящим текстом или файл .docx."
        )

    lines: List[str] = []
    for chunk in chunks:
        for line in chunk.splitlines():
            line = line.strip()
            if line:
                lines.append(line)

    logger.info("pdf %s: страниц %d, строк %d", path.name, pages, len(lines))
    return lines
