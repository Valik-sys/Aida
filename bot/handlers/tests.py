from __future__ import annotations

import datetime as dt
import logging
import random
import re
from typing import Any, Dict, List, Optional, Tuple

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.keyboards.inline import (
    ALL_MENU_BUTTONS,
    REASON_PREFIX,
    REPORT_PREFIX,
    UNSORTED_KEY,
    UNSORTED_LABEL,
    back_to_mode_kb,
    explain_kb,
    quantity_kb,
    question_report_kb,
    report_reasons_kb,
    reported_open_kb,
    place_start_kb,
    sections_kb,
    student_topics_kb,
    short_text,
    test_result_kb,
    tests_root_kb,
    variants_kb,
)
from bot.states.states import TestFlow
from database.db import (
    add_mistake,
    add_question_report,
    count_due,
    get_question_status,
    get_schedule,
    get_user,
    question_hash,
    record_answer,
    set_question_status,
)
from services.answer_explainer import explain_answer
from services.llm import (
    VERDICT_CORRECT,
    VERDICT_UNKNOWN,
    VERDICT_WRONG,
    check_test_answer,
    verdict_of,
)
from services import content_provider, reports, sections as sections_lib, teacher_content

logger = logging.getLogger(__name__)


router = Router()

# Формат реального билета ЦТ по истории Беларуси
TICKET_PART_A = 16
TICKET_PART_B = 22

# Показывается, когда преподаватель ещё не загрузил свои билеты
# и тренажёр работает на общей базе вопросов по предмету.
FALLBACK_NOTE = "ℹ️ Пока занимаемся на общих вопросах курса."


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)



# Отдельная кнопка разбора отключена: объяснение приходило отдельным
# сообщением уже после следующего вопроса и путало ученика. Короткое
# объяснение и так входит в ответ проверки (services/llm.py).
# Код разбора остался в services/answer_explainer.py — если понадобится
# развёрнутый разбор, включается сменой флага на True.
EXPLAIN_ERRORS_ENABLED = False


def _is_custom(key: str) -> bool:
    """Свой раздел преподавателя: «c1», «c2»."""
    return key.startswith(sections_lib.CUSTOM_PREFIX) and key[len(sections_lib.CUSTOM_PREFIX):].isdigit()


def _match_section(row_section: str, key) -> bool:
    """Попадает ли вопрос в выбранный раздел.

    Пустой ключ — «смешанные вопросы»: те, которым раздел не назначен.
    """
    s = (row_section or "").strip().lower()
    k = str(key).strip().lower()

    if not k:
        return not s
    if not s:
        return False
    if s == k or s == f"раздел {k}":
        return True
    # Тему сверяем только точно: «6_1» — это ровно она, а не соседняя «6_10».
    if sections_lib.is_topic(k):
        return False
    # Раздел вбирает в себя свои темы: выбрав раздел 6, ученик получает
    # и вопросы, разложенные по темам внутри него.
    if sections_lib.is_topic(s):
        return sections_lib.section_of(s) == k
    # Свои разделы сверяем только точно: иначе «c1» попадал бы в раздел 1
    # по вхождению цифры, и чужие вопросы утекали бы в сетку программы.
    if _is_custom(s) or _is_custom(k):
        return False
    # Старые данные вида «Раздел 3, тема 2» — по вхождению номера
    return k.isdigit() and k in s


def _get_row_variant(row: Dict[str, str], idx: int) -> str:
    key = f"Вар.{idx}"
    return (row.get(key) or "").strip()


def _is_real_option(text: str) -> bool:
    """Проверяет, что вариант ответа — настоящий текст, а не шаблон типа '1) 2) 3) 4)'."""
    cleaned = re.sub(r"[\d\)\(\s,;.\-]+", "", text.strip())
    return len(cleaned) > 0


def _normalize_expected_part_a(answer: str) -> str:
    m = re.search(r"\d+", answer or "")
    return m.group(0) if m else ""


_latin_to_cyr = str.maketrans({"A": "А", "B": "Б", "C": "В", "D": "Г"})


def detect_part_b_type(expected_answer: str) -> str:
    s = (expected_answer or "").strip().upper()
    s = s.translate(_latin_to_cyr)
    has_digits = bool(re.search(r"\d", s))
    has_letters = bool(re.search(r"[А-Е]", s))
    only_digits = bool(re.fullmatch(r"\d+", s)) if s else False
    only_letters = bool(re.fullmatch(r"[А-Е]+", s)) if s else False

    if has_digits and has_letters:
        return "correspondence"
    if only_letters:
        return "sequence"
    if only_digits:
        return "multiple"
    return "sequence"


def normalize_part_b_input(user_text: str, b_type: str) -> Tuple[bool, str]:
    raw = (user_text or "").strip()
    raw = raw.replace(",", " ").replace(";", " ").replace("-", " ").replace("\n", " ")
    raw = re.sub(r"\s+", "", raw).upper()
    raw = raw.translate(_latin_to_cyr)

    if b_type == "correspondence":
        if not re.search(r"\d", raw) or not re.search(r"[А-Е]", raw):
            return False, ""
        cleaned = re.sub(r"[^А-Е0-9]", "", raw)
        return (bool(cleaned), cleaned)

    if b_type == "sequence":
        cleaned = re.sub(r"[^А-Е]", "", raw)
        if not cleaned or any(ch.isdigit() for ch in raw):
            return False, ""
        return True, cleaned

    if b_type == "sequence_digits":
        digits = re.findall(r"\d+", raw)
        picked: List[str] = []
        for d in digits:
            picked.extend(list(d))
        picked = [x for x in picked if x.isdigit()]
        if not picked:
            return False, ""
        return True, "".join(picked)

    if b_type == "multiple":
        digits = re.findall(r"\d+", raw)
        picked: List[str] = []
        for d in digits:
            picked.extend(list(d))
        picked = [x for x in picked if x.isdigit() and int(x) >= 1]
        if not picked:
            return False, ""
        uniq = sorted(set(picked), key=lambda x: int(x))
        return True, "".join(uniq)

    return False, ""


def _normalize_expected_part_b(expected_answer: str) -> str:
    raw = (expected_answer or "").strip()
    raw = raw.replace(",", " ").replace(";", " ").replace("-", " ").replace("\n", " ")
    raw = re.sub(r"\s+", "", raw).upper()
    raw = raw.translate(_latin_to_cyr)
    # оставляем только А-Е и цифры — остальное выбрасываем
    return re.sub(r"[^А-Е0-9]", "", raw)


def _build_part_b_prompt(b_type: str) -> str:
    if b_type == "correspondence":
        return "Напиши ответ в формате `А3Б2В1` (без пробелов)."
    if b_type == "sequence":
        return "Напиши ответ в формате `БВГА` (без пробелов)."
    if b_type == "sequence_digits":
        return "Напиши ответ в формате `1234` (без пробелов)."
    return "Напиши ответ в формате `245` (цифры через пробел/запятую тоже можно)."


def _normalize_latin_markers(text: str) -> str:
    """Заменяет латинские буквы-маркеры на кириллические аналоги.
    A→А, B→В (латинская B визуально похожа на кириллическую В, не Б).
    """
    text = re.sub(r"(?<![А-ЯЁа-яёA-Za-z])A\)", "А)", text)
    text = re.sub(r"(?<![А-ЯЁа-яёA-Za-z])B\)", "В)", text)
    text = re.sub(r"(?<![А-ЯЁа-яёA-Za-z])C\)", "С)", text)
    text = re.sub(r"(?<![А-ЯЁа-яёA-Za-z])D\)", "Д)", text)
    text = re.sub(r"(?<![А-ЯЁа-яёA-Za-z])E\)", "Е)", text)
    return text


def _strip_answer_tail(text: str) -> str:
    """Удаляет хвостовые инструкции вида 'Ответ запишите...' и финальное 'Ответ:'."""
    cleaned_lines: List[str] = []
    for raw_line in text.splitlines():
        line = re.sub(r"\s*Ответ\s+запишите.*?(?=\s*«|$)", "", raw_line, flags=re.IGNORECASE).rstrip()
        line = re.sub(r"\s*Ответ\s*:?\s*$", "", line, flags=re.IGNORECASE).rstrip()
        line = re.sub(r"\s*\(?\s*Ответ\s*:\s*[^)]*\)?\s*$", "", line, flags=re.IGNORECASE).rstrip()
        cleaned_lines.append(line)

    while cleaned_lines and not cleaned_lines[-1].strip():
        cleaned_lines.pop()

    return "\n".join(cleaned_lines).strip()


def _split_inline_markers(text: str, *, split_letters: bool, split_numbers: bool) -> str:
    if split_letters:
        text = re.sub(r"(?<!\n)\s+([А-Е]\))", r"\n\1", text)
    if split_numbers:
        text = re.sub(r"(?<!\n)\s+(\d+[).])", r"\n\1", text)
    return text


def _build_sequence_example(text: str) -> str:
    letters = list(dict.fromkeys(re.findall(r"(?<![А-ЯЁ])([А-Е])\)", _normalize_latin_markers(text))))
    if letters:
        if len(letters) == 1:
            return letters[0]
        return "".join(letters[1:] + letters[:1])

    digits = list(dict.fromkeys(re.findall(r"(?<!\d)([1-9])[).]", text)))
    if digits:
        if len(digits) == 1:
            return digits[0]
        return "".join(digits[1:] + digits[:1])

    return "БВГА"


def _cleanup_display_line(line: str) -> str:
    line = line.strip()
    line = re.sub(r'\s*"+\s*$', "", line)
    line = re.sub(r":\.$", ":", line)
    return line


def _check_partial_credit(q: Dict[str, Any], raw_text: str) -> Optional[str]:
    """Частичный зачёт для вопросов с несколькими правильными ответами.

    Работает для:
    - type A (варианты 1-5): студент выбрал часть верных
    - type B, вид multiple (нумерованный список): студент выбрал часть верных

    Возвращает сообщение 🟡 если ученик выбрал только правильные варианты,
    но не все. Возвращает None во всех остальных случаях (LLM проверит сам).
    """
    q_type = q.get("type")

    if q_type == "A":
        expected = q.get("expected") or ""
        expected_digits = {int(ch) for ch in expected if ch.isdigit()}
        if len(expected_digits) <= 1:
            return None  # один ответ — без частичного зачёта

        seen: set = set()
        student_digits = []
        for ch in raw_text:
            if ch.isdigit() and 1 <= int(ch) <= 5 and int(ch) not in seen:
                seen.add(int(ch))
                student_digits.append(int(ch))
        student_set = set(student_digits)

        if not student_set or student_set == expected_digits:
            return None  # пустой ввод или полностью верно — LLM разберётся

        wrong_chosen = student_set - expected_digits
        if wrong_chosen:
            return None  # есть неверные — LLM даст ❌ с объяснением

        # Все выбранные верны, но не все выбраны → 🟡
        options = q.get("options") or {}
        correct_parts = [
            f"{d}) {options.get(d, '').strip()}" if (options.get(d) or "").strip() else str(d)
            for d in sorted(expected_digits)
        ]
        return (
            f"🟡 Частично верно! Ты выбрал {len(student_set)} из {len(expected_digits)} правильных ответов.\n"
            f"Полный правильный ответ: {'; '.join(correct_parts)}"
        )

    if q_type == "B" and _detect_part_b_kind(q) == "multiple":
        expected = q.get("expected") or ""
        norm_expected = _normalize_expected_part_b(expected)
        expected_set = {ch for ch in norm_expected if ch.isdigit()}
        if len(expected_set) <= 1:
            return None

        ok, norm_student = normalize_part_b_input(raw_text, "multiple")
        if not ok or not norm_student:
            return None
        student_set_b = {ch for ch in norm_student if ch.isdigit()}

        if not student_set_b or student_set_b == expected_set:
            return None  # пустой или полностью верно

        wrong = student_set_b - expected_set
        if wrong:
            return None  # есть неверные

        # Все выбранные верны, но не все → 🟡
        return (
            f"🟡 Частично верно! Ты выбрал {len(student_set_b)} из {len(expected_set)} правильных ответов.\n"
            f"Полный правильный ответ: {', '.join(sorted(expected_set))}"
        )

    return None


def _build_multiple_example(expected_answer: str) -> str:
    digits = [ch for ch in _normalize_expected_part_b(expected_answer) if ch.isdigit()]
    count = len(digits)
    if count <= 1:
        return "246"
    if count == 2:
        return "24"
    if count == 3:
        return "246"
    if count == 4:
        return "1246"
    return "12345"


def _restore_missing_sequence_markers(lines: List[str]) -> List[str]:
    if not lines:
        return lines

    marker_kind = None
    for line in lines:
        if re.match(r"^[А-Е]\)", line):
            marker_kind = "letters"
            break
        if re.match(r"^\d+\)", line):
            marker_kind = "numbers"
            break

    if marker_kind is None:
        return lines

    if marker_kind == "letters":
        alphabet = list("АБВГДЕ")
        first_labeled = next(
            ((i, alphabet.index(m.group(1))) for i, line in enumerate(lines) if (m := re.match(r"^([А-Е])\)\s*(.*)$", line))),
            None,
        )
        if first_labeled is None:
            return lines
        next_idx = max(0, first_labeled[1] - first_labeled[0])
        restored: List[str] = []
        def _next_labeled_letter(start_pos: int) -> Optional[str]:
            for future in lines[start_pos + 1:]:
                m_future = re.match(r"^([А-Е])\)\s*(.*)$", future)
                if m_future:
                    return m_future.group(1)
            return None
        for pos, line in enumerate(lines):
            m = re.match(r"^([А-Е])\)\s*(.*)$", line)
            if m:
                next_idx = alphabet.index(m.group(1)) + 1
                restored.append(f"{m.group(1)}) {m.group(2).strip()}")
            else:
                expected_letter = alphabet[next_idx] if next_idx < len(alphabet) else None
                next_labeled = _next_labeled_letter(pos)
                if restored and re.match(r"^[а-яё]", line) and expected_letter and next_labeled == expected_letter:
                    restored[-1] = f"{restored[-1]} {line.strip()}".strip()
                    continue
                if restored and re.match(r"^[-—]", line):
                    restored[-1] = f"{restored[-1]} {line.strip()}".strip()
                    continue
                if next_idx >= len(alphabet):
                    restored.append(line)
                else:
                    restored.append(f"{alphabet[next_idx]}) {line}")
                    next_idx += 1
        return restored

    first_labeled_num = next(
        ((i, int(m.group(1))) for i, line in enumerate(lines) if (m := re.match(r"^(\d+)\)\s*(.*)$", line))),
        None,
    )
    if first_labeled_num is None:
        return lines
    next_num = max(1, first_labeled_num[1] - first_labeled_num[0])
    restored = []
    for line in lines:
        m = re.match(r"^(\d+)\)\s*(.*)$", line)
        if m:
            next_num = int(m.group(1)) + 1
            restored.append(f"{m.group(1)}) {m.group(2).strip()}")
        else:
            if restored and re.match(r"^[-—]", line):
                restored[-1] = f"{restored[-1]} {line.strip()}".strip()
                continue
            restored.append(f"{next_num}) {line}")
            next_num += 1
    return restored


def _clean_question_text(raw: str) -> str:
    """Обрезает склеенные вопросы — оставляет только первый."""
    m = re.search(r"Ответ\s*:\s*[АБВ]\d+[\.\s]", raw)
    if m:
        raw = raw[: m.start()]
    lines = raw.split("\n")
    # Убираем строки которые ТОЛЬКО являются меткой "Ответ:" или пустыми слотами "1) 2) 3)"
    while lines and (
        re.fullmatch(r"(\d+\)\s*)+", lines[-1].strip())
        or re.fullmatch(r"[Оо]твет\s*:?\s*", lines[-1].strip())
    ):
        lines.pop()
    # Обрезаем хвостовое " Ответ:" если оно прилеплено к строке с содержимым
    if lines:
        lines[-1] = re.sub(r"\s*[Оо]твет\s*:?\s*$", "", lines[-1]).rstrip()
        if not lines[-1]:
            lines.pop()
    return "\n".join(lines).strip()


def _is_usable_question_text(text: str) -> bool:
    cleaned = _clean_question_text(text or "").strip()
    if not cleaned:
        return False
    meaningful = re.sub(r"[\W_]+", "", cleaned, flags=re.UNICODE)
    if len(meaningful) <= 1:
        return False
    return True


def _is_usable_question(q: Dict[str, Any]) -> bool:
    question_text = q.get("question_text") or ""
    if not _is_usable_question_text(question_text):
        return False

    if q.get("type") == "B":
        kind = _detect_part_b_kind(q)
        if kind == "sequence":
            preview_text = _format_sequence_question(
                _normalize_latin_markers(_clean_question_text(question_text))
            )
            digit_markers = re.findall(r"(?m)^(\d+)\)", preview_text)
            if len(digit_markers) != len(set(digit_markers)):
                return False
            letter_markers = re.findall(r"(?m)^([А-Е])\)", preview_text)
            if len(letter_markers) != len(set(letter_markers)):
                return False

    return True


def _detect_part_b_kind(q: Dict[str, Any]) -> str:
    raw_text = q.get("question_text") or ""
    text = _normalize_latin_markers(_clean_question_text(raw_text))
    lower = text.lower()
    raw_lower = raw_text.lower()
    line_count = len([line for line in text.splitlines() if line.strip()])
    sequence_like = bool(re.search(r"(последоват|хронологическ|расстав|располож)", lower))
    has_numbered_options = bool(re.search(r"(?m)^\d+[).]", _split_inline_markers(text, split_letters=False, split_numbers=True)))
    explicit_multiple = (
        "порядок записи цифр не имеет значения" in raw_lower
        or "например: 123" in raw_lower
        or "например: 135" in raw_lower
        or "например: 245" in raw_lower
        or "например: 246" in raw_lower
    )
    explicit_year = (
        "четырехзначного числа" in raw_lower
        or "четырёхзначного числа" in raw_lower
        or "например: 1900" in raw_lower
    )

    if "соотнес" in lower or "соответств" in lower:
        return "correspondence"
    if sequence_like:
        return "sequence"
    if ("дату" in raw_lower or "(год)" in raw_lower or " год " in raw_lower or "году" in raw_lower or "года" in raw_lower) and explicit_year:
        return "year_open"
    if explicit_year and not has_numbered_options:
        return "year_open"
    if has_numbered_options and explicit_multiple:
        return "multiple"
    if re.search(r"\b(выберите|укажите|отметьте)\b", lower) and line_count >= 3 and not sequence_like:
        return "multiple"
    return "open"


def _get_part_b_input_type(q: Dict[str, Any]) -> Optional[str]:
    kind = _detect_part_b_kind(q)
    if kind == "correspondence":
        return "correspondence"
    if kind == "sequence":
        preview_text = _format_sequence_question(
            _normalize_latin_markers(_clean_question_text(q.get("question_text") or ""))
        )
        has_letter_markers = bool(re.search(r"(?m)^[А-Е]\)", preview_text))
        has_digit_markers = bool(re.search(r"(?m)^\d+\)", preview_text))
        if has_digit_markers and not has_letter_markers:
            return "sequence_digits"
        return "sequence"
    if kind == "multiple":
        return kind
    return None


def _normalize_year_input(user_text: str) -> Tuple[bool, str]:
    digits = re.findall(r"\d+", user_text or "")
    if len(digits) != 1:
        return False, ""
    year = digits[0]
    if len(year) not in {3, 4}:
        return False, ""
    return True, year


def _detect_hint(q: Dict[str, Any]) -> str:
    """Автоопределение подсказки по содержимому вопроса."""
    text = _clean_question_text(q.get("question_text") or "").lower()
    has_options = q["type"] == "A" and any(
        (q.get("options") or {}).get(i, "").strip() for i in range(1, 6)
    )
    # Множественный выбор с вариантами
    if has_options:
        expected = (q.get("expected") or "").strip()
        digits = [ch for ch in expected if ch.isdigit()]
        opts = q.get("options") or {}
        max_option = max((i for i in range(1, 6) if _is_real_option(opts.get(i, ""))), default=5)
        if len(digits) > 1:
            example = "".join(str(d) for d in digits[:3]) or "13"
            example_spaced = ", ".join(str(d) for d in digits[:3]) or "1, 3"
            return f"✏️ Напиши номера ответов, например: {example} или {example_spaced}."
        return f"✏️ Напиши номер ответа (1–{max_option})."
    if q["type"] == "B":
        kind = _detect_part_b_kind(q)
        raw_text = q.get("question_text") or ""
        if kind == "correspondence":
            preview_text = _parse_and_reformat_correspondence(
                _normalize_latin_markers(_clean_question_text(raw_text))
            )
            letters = list(dict.fromkeys(re.findall(r"(?<![А-ЯЁ])([А-Е])\)", preview_text)))
            numbers = list(dict.fromkeys(re.findall(r"(?m)^(\d+)\)", preview_text)))
            expected = _normalize_expected_part_b(q.get("expected") or "")
            pair_count = len(re.findall(r"[А-Е]\d", expected))
            if not letters:
                letters = ["А", "Б", "В", "Г"]
            if not numbers:
                numbers = [str(i + 1) for i in range(len(letters))]
            if pair_count:
                letters = letters[:pair_count]
            _lo = "АБВГДЕ"
            letters.sort(key=lambda c: _lo.index(c) if c in _lo else 99)
            example = "".join(f"{l}{numbers[i % len(numbers)]}" for i, l in enumerate(letters))
            return f"✏️ Напиши соответствие, например: {example}"
        if kind == "sequence":
            preview_text = _format_sequence_question(
                _normalize_latin_markers(_clean_question_text(raw_text))
            )
            example = _build_sequence_example(preview_text)
            if re.search(r"\b[1-9][).]", preview_text) and not re.search(r"[АБВГДЕ]\)", preview_text):
                return f"✏️ Напиши последовательность цифрами, например: {example}"
            return f"✏️ Напиши последовательность, например: {example}"
        if kind == "year_open":
            return "✏️ Напиши год, например: 1900."
        if kind == "multiple":
            example = _build_multiple_example(q.get("expected") or "")
            example_spaced = ", ".join(list(example))
            return f"✏️ Напиши номера ответов, например: {example} или {example_spaced}."
    # Заполнение пропуска (есть подчёркивания)
    if "___" in (q.get("question_text") or "") or "пропуск" in text:
        return "✏️ Напиши ответ."
    # По умолчанию
    return "✏️ Напиши ответ."



def _parse_and_reformat_correspondence(text: str) -> str:
    """Приводит задания на соответствие к виду 'левый столбец + пронумерованный правый'."""
    text = _strip_answer_tail(text)
    text = _split_inline_markers(text, split_letters=True, split_numbers=True)

    header_lines: List[str] = []
    letter_items: List[str] = []
    number_items: List[str] = []
    normalized_lines: List[str] = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        line = re.sub(r"^([1-9])(?=[А-ЯЁа-яёA-Za-z])", r"\1) ", line)
        line = re.sub(r"^([1-9])\s+(?=\S)", r"\1) ", line)
        line = re.sub(r"^(\d+)[.]\s*", r"\1) ", line)
        line = re.sub(r"^(\d+)[)]\s*", r"\1) ", line)
        line = re.sub(r"^([А-Е])\)\s*", r"\1) ", line)
        if (
            normalized_lines
            and any(re.match(r"^[А-Е]\)", prev) for prev in normalized_lines)
            and re.match(r"^[А-Е][А-ЯЁа-яё]+[.;:]?$", line)
            and " " not in line
            and not re.match(r"^[А-Е]\)", line)
        ):
            line = re.sub(r"^([А-Е])", r"\1) ", line, count=1)
        line = _cleanup_display_line(line.rstrip(" ;,"))
        if re.match(r"^(?:[А-Е]|\d+)\)", line):
            line = re.sub(r"\s*[—–-]\s*$", "", line).strip()
        if line:
            normalized_lines.append(line)

    def _append_continuation(items: List[str], extra: str) -> None:
        if not items:
            return
        items[-1] = f"{items[-1]} {extra.strip()}".strip()

    def _next_marked_kind(start_idx: int) -> Optional[str]:
        for future in normalized_lines[start_idx + 1:]:
            if re.match(r"^[А-Е]\)", future):
                return "letters"
            if re.match(r"^\d+\)", future):
                return "numbers"
        return None

    mode = "header"
    for idx, line in enumerate(normalized_lines):
        if re.match(r"^[А-Е]\)", line):
            letter_items.append(line)
            mode = "letters"
            continue

        if re.match(r"^\d+\)", line):
            number_items.append(line)
            mode = "numbers"
            continue

        next_kind = _next_marked_kind(idx)
        starts_lower = bool(re.match(r"^[а-яё]", line))

        if mode == "letters":
            if next_kind == "letters" or starts_lower:
                _append_continuation(letter_items, line)
            else:
                number_items.append(line)
                mode = "numbers_unmarked"
            continue

        if mode == "numbers":
            _append_continuation(number_items, line)
            continue

        if mode == "numbers_unmarked":
            if starts_lower and number_items:
                _append_continuation(number_items, line)
            else:
                number_items.append(line)
            continue

        if letter_items or number_items:
            number_items.append(line)
        else:
            header_lines.append(line)

    if not letter_items:
        return text

    normalized_number_items: List[str] = []
    next_num = 1
    for item in number_items:
        m = re.match(r"^(\d+)\)\s*(.*)$", item)
        if m and not m.group(2).strip():
            continue
        if re.fullmatch(r"\d+[.)]?", item.strip()):
            continue
        if m:
            next_num = max(next_num, int(m.group(1)) + 1)
            cleaned = _cleanup_display_line(m.group(2).strip().rstrip(" ;,"))
            cleaned = re.sub(r"\s*[—–-]\s*$", "", cleaned).strip()
            if not cleaned:
                continue
            normalized_number_items.append(f"{m.group(1)}) {cleaned}")
        else:
            cleaned = _cleanup_display_line(item.strip().rstrip(" ;,"))
            cleaned = re.sub(r"\s*[—–-]\s*$", "", cleaned).strip()
            if not cleaned:
                continue
            normalized_number_items.append(f"{next_num}) {cleaned}")
            next_num += 1

    result_lines: List[str] = []
    if header_lines:
        result_lines.extend(header_lines)
        result_lines.append("")
    result_lines.extend(letter_items)
    if normalized_number_items:
        result_lines.append("")
        result_lines.extend(normalized_number_items)

    return "\n".join(result_lines).strip()


def _format_sequence_question(text: str) -> str:
    text = _strip_answer_tail(text)
    text = _split_inline_markers(text, split_letters=True, split_numbers=True)
    text = re.sub(r"(?m)^([А-Е])\)\s*", r"\1) ", text)
    text = re.sub(r"(?m)^([1-9])(?=[А-ЯЁа-яёA-Za-z])", r"\1) ", text)
    text = re.sub(r"(?m)^(\d+)[.)]\s*", r"\1) ", text)
    text = re.sub(r"(?m)^([1-9])\s+(?=\S)", r"\1) ", text)

    lines = [_cleanup_display_line(line.strip().rstrip(";")) for line in text.splitlines() if line.strip()]
    if len(lines) > 1:
        lines = [lines[0]] + _restore_missing_sequence_markers(lines[1:])
    return "\n".join(lines).strip()


def _format_multiple_choice_question(text: str) -> str:
    text = _strip_answer_tail(text)
    text = _split_inline_markers(text, split_letters=False, split_numbers=True)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) <= 1:
        return text

    def _line_number(line: str) -> Optional[int]:
        match = re.match(r"^(\d+)[.)]?\s*\S", line)
        return int(match.group(1)) if match else None

    option_start = 1
    candidate_starts: List[int] = []
    for idx, line in enumerate(lines[1:], start=1):
        if _line_number(line) != 1:
            continue
        later_numbers = {_line_number(item) for item in lines[idx + 1:] if _line_number(item) is not None}
        if 2 in later_numbers:
            candidate_starts.append(idx)
    if candidate_starts:
        option_start = candidate_starts[-1]

    header_lines = [_cleanup_display_line(line.rstrip(" ;")) for line in lines[:option_start]]
    header_lines = [line for line in header_lines if line]
    header = "\n".join(header_lines).strip()
    options = lines[option_start:]
    numbered: List[str] = []
    next_num = 1

    for option in options:
        cleaned = re.sub(r"^[-•]\s*", "", option).strip()
        match = re.match(r"^(\d+)[.)]?\s*(.*)$", cleaned)
        if match and not match.group(2).strip():
            continue
        if re.fullmatch(r"\d+[.)]?", cleaned):
            continue
        if match and match.group(2):
            number = int(match.group(1))
            next_num = max(next_num, number + 1)
            numbered.append(f"{number}) {_cleanup_display_line(match.group(2).strip().rstrip(' ;,'))}")
        else:
            cleaned = _cleanup_display_line(cleaned.rstrip(" ;,"))
            if not cleaned:
                continue
            numbered.append(f"{next_num}) {cleaned}")
            next_num += 1

    if not numbered:
        return header or text
    if not header:
        return "\n".join(numbered)
    return f"{header}\n\n" + "\n".join(numbered)


PROGRESS_WIDTH = 10
PROGRESS_FILLED = "▰"
PROGRESS_EMPTY = "▱"
OPTION_DIGITS = {1: "1️⃣", 2: "2️⃣", 3: "3️⃣", 4: "4️⃣", 5: "5️⃣"}


def _progress_bar(pos: int, total: int) -> str:
    if total <= 0:
        return ""
    filled = max(1, round(PROGRESS_WIDTH * pos / total))
    return PROGRESS_FILLED * filled + PROGRESS_EMPTY * (PROGRESS_WIDTH - filled)


def _question_header(q: Dict[str, Any], pos: int, total: int) -> str:
    part = "часть А" if q.get("type") == "A" else "часть Б"
    return f"🎯 {pos}/{total} · {part}\n{_progress_bar(pos, total)}"


def _format_question(q: Dict[str, Any], pos: int, total: int) -> str:
    """Форматирует вопрос для красивого отображения в Telegram."""
    header = _question_header(q, pos, total)
    text = _normalize_latin_markers(_clean_question_text(q["question_text"] or ""))

    hint = _detect_hint(q)

    # Часть А с вариантами — выводим их текстом
    if q["type"] == "A":
        options = q.get("options") or {}
        opts_lines: List[str] = []
        for i in range(1, 6):
            opt = (options.get(i) or "").strip()
            if opt:
                opts_lines.append(f"{OPTION_DIGITS[i]} {opt}")
        if opts_lines:
            opts_block = "\n".join(opts_lines)
            return f"{header}\n\n{text}\n\n{opts_block}\n\n{hint}"

    # Часть B — визуальное форматирование
    kind = _detect_part_b_kind(q)
    if kind == "correspondence":
        text = _parse_and_reformat_correspondence(text)
    elif kind == "sequence":
        text = _format_sequence_question(text)
    elif kind == "multiple":
        text = _format_multiple_choice_question(text)
    else:
        text = _strip_answer_tail(text)
        text = re.sub(r"(?<!\n)\s+([А-Е]\))", r"\n\1", text)
        text = re.sub(r"(?<!\n)\s+(\d+[).])", r"\n\1", text)
        text = re.sub(r"(?<!\n)\s+(Ответ\s*запишите)", r"\n\1", text)
        text = re.sub(r"(?<!\n)\s+(Ответ\s*:)", r"\n\1", text)
        # Нормализуем "1Текст" → "1) Текст"
        text = re.sub(r"(?m)^(\d+)([^\)\s\d])", r"\1) \2", text)
        text = re.sub(r"(?m)^(\d+)[.)]\s*", r"\1) ", text)
        # Убираем лишние пробелы/табы после маркеров и хвостовые ";"
        text = re.sub(r"(?m)^([А-Е]\))\s{2,}", r"\1 ", text)
        text = re.sub(r"(?m);\s*$", "", text)

    return f"{header}\n\n{text}\n\n{hint}"


def _number_descriptions(text: str) -> str:
    """Нумерует описания (правый столбец) в вопросах на соответствие,
    только если они ещё не пронумерованы.
    """
    lines = text.split("\n")
    last_letter_idx = -1
    answer_idx = len(lines)

    for i, line in enumerate(lines):
        stripped = line.strip()
        if re.match(r"^[А-ЯЁ]\)", stripped):
            last_letter_idx = i
        if "ответ" in stripped.lower():
            answer_idx = i
            break

    if last_letter_idx < 0 or last_letter_idx + 1 >= answer_idx:
        return text

    # Пропускаем строки которые уже начинаются с цифры (любой формат: "1)", "1 ", "1Текст")
    desc_indices = [
        i for i in range(last_letter_idx + 1, answer_idx)
        if lines[i].strip() and not re.match(r"^\d+", lines[i].strip())
    ]

    if not desc_indices:
        return text

    num = 1
    for i in desc_indices:
        lines[i] = f"{num}) {lines[i].strip()}"
        num += 1

    return "\n".join(lines)
# Какую часть тренировки отдавать под повторение. Остальное — новый материал.
DUE_SHARE = 0.5


def _sort_by_priority(
    rows: List[Dict[str, str]],
    schedule: Dict[str, Dict[str, Any]],
    now_iso: str,
    limit: Optional[int] = None,
) -> List[Dict[str, str]]:
    """Раскладывает вопросы по важности для этого ученика.

    Порядок такой:
      1. пора повторить — срок подошёл, иначе забудется;
      2. новые — то, чего ученик ещё не видел;
      3. остальные — освоенное, срок повтора которого ещё не наступил.

    Повторы занимают **не больше половины** набора. Без этого ограничения
    ученик с полусотней ошибок гонял бы их по кругу и никогда не добрался
    бы до нового материала — а для работы над ошибками есть отдельный режим.

    Внутри повторов первыми идут те, что ждут дольше всех: так вся копилка
    прокручивается равномерно. Новые и освоенные перемешиваются, чтобы
    тренировки не повторялись одна в одну.
    """
    due: List[Dict[str, str]] = []
    fresh: List[Dict[str, str]] = []
    rest: List[Dict[str, str]] = []

    for row in rows:
        stat = schedule.get(question_hash(row.get("Вопрос") or ""))
        if stat is None:
            fresh.append(row)
        elif (stat["due_at"] or "") <= now_iso:
            due.append(row)
        else:
            rest.append(row)

    # Дольше всех ждущие — вперёд
    due.sort(key=lambda r: schedule[question_hash(r.get("Вопрос") or "")]["due_at"] or "")
    random.shuffle(fresh)
    random.shuffle(rest)

    if limit:
        max_due = max(1, round(limit * DUE_SHARE))
        # Оставшиеся повторы не выбрасываем: если нового материала не хватит,
        # добор пойдёт из них
        return due[:max_due] + fresh + due[max_due:] + rest

    return due + fresh + rest


def _skipped_positions(questions: List[Dict[str, Any]]) -> set:
    """Вопросы, отмеченные учеником как сломанные."""
    return {i for i, q in enumerate(questions) if q.get("skipped")}


def _calc_result(questions: List[Dict[str, Any]], wrong: List[int]) -> Tuple[int, int]:
    """Верных и всего. Пропущенные по жалобе не считаются ни там, ни там.

    Ученик на них не отвечал — записывать верными было бы враньём. А если
    ответ был и оказался неверным, ошибку тоже снимаем: вопрос сломан,
    и виноват не ученик.
    """
    skipped = _skipped_positions(questions)
    total = len(questions) - len(skipped)
    real_wrong = [i for i in wrong if i not in skipped]
    correct = total - len(real_wrong)
    return correct, total


def _result_screen(correct: int, total: int, wrong_positions: List[str]) -> str:
    if not total:
        # Все вопросы набора ученик отметил как спорные — считать нечего
        return (
            "🏆 Тренировка завершена\n\n"
            "Отвеченных вопросов не осталось — все отмечены как спорные.\n"
            "Преподаватель их посмотрит."
        )

    share = correct / total if total else 0
    if share == 1:
        verdict = "Идеально — ни одной ошибки."
    elif share >= 0.8:
        verdict = "Сильный результат."
    elif share >= 0.5:
        verdict = "Есть над чем поработать."
    else:
        verdict = "Тему стоит пройти заново."

    lines = [
        "🏆 Тренировка завершена",
        _progress_bar(correct, total),
        f"Верно: {correct} из {total}",
        verdict,
    ]
    if wrong_positions:
        lines.append(f"\nОшибки в вопросах: {', '.join(wrong_positions)}")
    return "\n".join(lines)


def _available_sections(bundle) -> List[Tuple[str, str]]:
    """Разделы, в которых у этого ученика реально есть вопросы.

    Сюда попадают и свои разделы преподавателя, и «смешанные вопросы» —
    те, которым раздел не назначен. Обычно это полные билеты, где разделы
    перемешаны: назначить им один нельзя, а тренироваться на них нужно.
    """
    # Вопрос, разложенный по теме, считается и за свой раздел: иначе раздел,
    # где всё разложено по темам, выглядел бы пустым и в список бы не попал.
    counts: Dict[str, int] = {}
    for row in bundle.rows:
        key = sections_lib.section_of((row.get("Раздел") or "").strip())
        counts[key] = counts.get(key, 0) + 1

    custom: Dict[str, str] = {}
    if bundle.teacher_id:
        custom = teacher_content.custom_sections(bundle.teacher_id, bundle.subject)

    out: List[Tuple[str, str]] = []
    for section in sections_lib.merged(bundle.subject, custom):
        if not counts.get(section.key):
            continue
        # У разделов программы название полное — ученик узнаёт его по учебнику
        full = next(
            (s.full for s in sections_lib.base_sections(bundle.subject) if s.key == section.key),
            "",
        )
        out.append((section.key, full or section.label))

    if counts.get(""):
        out.append((UNSORTED_KEY, UNSORTED_LABEL))

    return out


@router.callback_query(lambda c: c.data == "tests:by_section")  # type: ignore[call-arg]
async def by_section(callback: CallbackQuery, state: FSMContext) -> None:
    bundle = await content_provider.get_tests(callback.from_user.id)
    available = _available_sections(bundle)

    if not available:
        await callback.message.edit_text(
            bundle.empty_reason or "Разделов пока нет — вопросы ещё не разложены."
        )
        await callback.answer()
        return

    await state.update_data(test_filter_type="section", test_filter_value=None, test_qty=None)
    await callback.message.edit_text(
        "Выбери раздел:", reply_markup=sections_kb(available)
    )
    await callback.answer()


@router.callback_query(lambda c: c.data == "tests:by_variant")  # type: ignore[call-arg]
async def by_variant(callback: CallbackQuery, state: FSMContext) -> None:
    bundle = await content_provider.get_tests(callback.from_user.id)
    variants = content_provider.variants_of(bundle.rows)
    if not variants:
        await callback.message.edit_text(bundle.empty_reason or "Вариантов пока нет.")
        await callback.answer()
        return
    await state.update_data(test_filter_type="variant", test_filter_value=None, test_qty=None)
    await callback.message.edit_text("Выбери вариант:", reply_markup=variants_kb(variants))
    await callback.answer()


# Раздел строится: сюда лягут настоящие тесты прошлых лет, общие для всех.
# Пока экран-заглушка — чтобы кнопка не выглядела сломанной.
ARCHIVE_STUB = (
    "📚 Тесты прошлых лет\n\n"
    "Здесь появятся настоящие тесты прошлых лет — "
    "целый вариант, как на экзамене.\n\n"
    "Пока пусто, скоро добавим."
)


@router.callback_query(lambda c: c.data == "tests:archive")  # type: ignore[call-arg]
async def by_archive(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(test_filter_type=None, test_filter_value=None, test_qty=None)
    await callback.message.edit_text(ARCHIVE_STUB, reply_markup=back_to_mode_kb("tests"))
    await callback.answer()


@router.callback_query(lambda c: c.data == "tests:part_b")  # type: ignore[call-arg]
async def by_part_b(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(test_filter_type="part_b", test_filter_value=None, test_qty=None)
    await callback.message.edit_text("Сколько вопросов части Б нужно?", reply_markup=quantity_kb())
    await callback.answer()


@router.callback_query(lambda c: c.data == "tests:ticket")  # type: ignore[call-arg]
async def random_ticket(callback: CallbackQuery, state: FSMContext) -> None:
    """Полный билет по всем материалам ученика."""
    await _deal_ticket(callback, state)


@router.callback_query(lambda c: c.data and c.data.startswith("tests:secticket:"))  # type: ignore[call-arg]
async def section_ticket(callback: CallbackQuery, state: FSMContext) -> None:
    """Полный билет по одному разделу.

    Тот же билет, что в главном меню, только вопросы берутся из раздела:
    после разбора темы на занятии имеет смысл прогнать не пять вопросов,
    а целый билет — но по этому разделу, а не по всему курсу.
    """
    key = (callback.data or "").split(":")[-1].strip()
    if not key:
        await callback.answer()
        return
    await _deal_ticket(callback, state, section=key)


async def _deal_ticket(
    callback: CallbackQuery,
    state: FSMContext,
    section: Optional[str] = None,
) -> None:
    """Собирает билет: TICKET_PART_A вопросов части А + TICKET_PART_B части Б.

    `section` — собрать только из этого раздела или темы. Отбор тот же, что
    в тренировке по разделу, поэтому раздел вбирает и свои темы.
    """
    bundle = await content_provider.get_tests(callback.from_user.id)
    if not bundle:
        await callback.message.edit_text(bundle.empty_reason or "Тесты временно недоступны.")
        await callback.answer()
        return

    rows = bundle.rows
    if section:
        key = "" if section == UNSORTED_KEY else section
        rows = [r for r in rows if _match_section(r.get("Раздел", ""), key)]

    all_questions: List[Dict[str, Any]] = []
    for row in rows:
        question_text = row.get("Вопрос") or ""
        expected_raw = (row.get("Ответ") or "").strip()
        options = {i: _get_row_variant(row, i) for i in range(1, 6)}
        has_options = any(_is_real_option(options.get(i, "")) for i in range(1, 6))

        qt = question_text.strip()
        if not has_options and not expected_raw and not qt:
            continue
        if not _is_usable_question(
            {
                "type": "A" if has_options else "B",
                "expected": expected_raw,
                "question_text": question_text,
                "options": options if has_options else {},
            }
        ):
            continue
        ql = qt.lower()
        if (("соответств" in ql or "соотнес" in ql or "последовательн" in ql or "хронологическ" in ql)
                and not re.search(r"[АБВГ]\)", qt) and len(qt) < 250):
            continue

        all_questions.append({
            "type": "A" if has_options else "B",
            "expected": expected_raw,
            "question_text": question_text,
            "options": options if has_options else {},
        })

    part_a = [q for q in all_questions if q["type"] == "A"]
    part_b = [q for q in all_questions if q["type"] == "B"]

    random.shuffle(part_a)
    random.shuffle(part_b)

    ticket = part_a[:TICKET_PART_A] + part_b[:TICKET_PART_B]

    if not ticket:
        await callback.message.edit_text("Не удалось сформировать билет.")
        await callback.answer()
        return

    actual_a = min(len(part_a), TICKET_PART_A)
    actual_b = min(len(part_b), TICKET_PART_B)

    await state.update_data(test_session={
        "questions": ticket, "idx": 0, "wrong": [],
        "teacher_id": bundle.teacher_id, "subject": bundle.subject,
    })
    note = f"{FALLBACK_NOTE}\n\n" if bundle.fallback else ""
    # В билете по разделу вопросов может не хватить на полный: обещать
    # 38 и молча выдать 12 нельзя, поэтому счёт частей показывается всегда
    head = "🎯 Полный билет"
    if section:
        where = sections_lib.title_of(bundle.subject, section)
        head = f"🎯 Билет: {UNSORTED_LABEL if section == UNSORTED_KEY else where}"
    await callback.message.edit_text(
        f"{note}{head}\n"
        f"Часть А: {actual_a} · часть Б: {actual_b} · всего: {len(ticket)}\n\n"
        "Поехали!"
    )
    await _send_question(callback.bot, callback.message.chat.id, state, idx=0, user_id=callback.from_user.id)
    await callback.answer()


def _available_topics(bundle, section: str) -> List[Tuple[str, str, int]]:
    """Темы этого раздела, в которых у ученика есть вопросы.

    Пустые темы не показываем по той же причине, что и пустые разделы:
    из пятидесяти семи тем программы у преподавателя обычно разложены три.
    """
    counts: Dict[str, int] = {}
    for row in bundle.rows:
        key = (row.get("Раздел") or "").strip()
        if sections_lib.is_topic(key) and sections_lib.section_of(key) == section:
            counts[key] = counts.get(key, 0) + 1

    if not counts:
        return []

    custom_topics: Dict[str, str] = {}
    if bundle.teacher_id:
        custom_topics = teacher_content.custom_topics(bundle.teacher_id, bundle.subject)

    out: List[Tuple[str, str, int]] = []
    for topic in sections_lib.merged_topics(bundle.subject, section, custom_topics):
        count = counts.get(topic.key)
        if count:
            out.append((topic.key, topic.title, count))
    return out


async def _show_topics(callback: CallbackQuery, bundle, section: str, topics) -> None:
    """Экран выбора темы внутри раздела."""
    await callback.message.edit_text(
        f"{sections_lib.title_of(bundle.subject, section)}\n\nВыбери тему:",
        reply_markup=student_topics_kb(topics, section),
    )


@router.callback_query(lambda c: c.data and c.data.startswith("tests:section:"))  # type: ignore[call-arg]
async def pick_section(callback: CallbackQuery, state: FSMContext) -> None:
    key = (callback.data or "").split(":")[-1].strip()
    if not key:
        await callback.answer()
        return

    # У «смешанных вопросов» тем нет по определению — там вопросы разных
    # разделов вперемешку.
    if key != UNSORTED_KEY and not sections_lib.is_topic(key):
        bundle = await content_provider.get_tests(callback.from_user.id)
        topics = _available_topics(bundle, key)
        if topics:
            await _show_topics(callback, bundle, key, topics)
            await callback.answer()
            return

    await _ask_quantity(callback, state, key)


def _place_words(key: str) -> Tuple[str, str]:
    """Как назвать выбранное место: (подпись билета, о чём вопрос на экране)."""
    if key == UNSORTED_KEY:
        return "Билет", "по смешанным вопросам"
    if sections_lib.is_topic(key):
        return "Билет по теме", "по этой теме"
    return "Билет по разделу", "по этому разделу"


async def _ask_quantity(callback: CallbackQuery, state: FSMContext, key: str) -> None:
    """Экран выбора: целый билет или своё количество вопросов.

    Один и тот же для раздела, темы и «Смешанных вопросов» — иначе билет
    оказывался бы то доступен, то нет без всякой причины.
    """
    await state.update_data(test_filter_type="section", test_filter_value=key, test_qty=None)
    ticket_text, about = _place_words(key)
    await callback.message.edit_text(
        f"Как тренируемся {about}?",
        reply_markup=place_start_kb(key, TICKET_PART_A + TICKET_PART_B, ticket_text),
    )
    await callback.answer()


@router.callback_query(lambda c: c.data == "tests:qty_back")  # type: ignore[call-arg]
async def qty_back(callback: CallbackQuery, state: FSMContext) -> None:
    """Шаг назад с экрана количества — туда, откуда на него пришли.

    Куда именно, видно по уже выбранному фильтру: раздел, тема или вариант.
    Отдельно запоминать путь не нужно, а если бы запоминали — он бы разъехался
    с настоящим при первом же новом входе на этот экран.
    """
    data = await state.get_data()
    filter_type = data.get("test_filter_type")
    value = str(data.get("test_filter_value") or "")

    if filter_type == "variant":
        await by_variant(callback, state)
        return

    if filter_type == "section" and value:
        if value != UNSORTED_KEY:
            bundle = await content_provider.get_tests(callback.from_user.id)
            section = sections_lib.section_of(value)
            topics = _available_topics(bundle, section)
            # Темы есть — значит через их список мы сюда и попали
            if topics:
                await _show_topics(callback, bundle, section, topics)
                await callback.answer()
                return
        await by_section(callback, state)
        return

    # Часть Б и всё прочее приходят прямо из корня «Тренировки»
    await state.update_data(test_filter_type=None, test_filter_value=None, test_qty=None)
    await callback.message.edit_text("Режим: Тесты", reply_markup=tests_root_kb())
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("tests:secall:"))  # type: ignore[call-arg]
async def pick_section_whole(callback: CallbackQuery, state: FSMContext) -> None:
    """Весь раздел целиком, минуя список тем."""
    key = (callback.data or "").split(":")[-1].strip()
    if not key:
        await callback.answer()
        return
    await _ask_quantity(callback, state, key)


@router.callback_query(lambda c: c.data and c.data.startswith("tests:variant_idx:"))  # type: ignore[call-arg]
async def pick_variant_idx(callback: CallbackQuery, state: FSMContext) -> None:
    idx_s = (callback.data or "").split(":")[-1]
    try:
        idx = int(idx_s)
    except Exception:  # noqa: BLE001
        await callback.answer()
        return
    bundle = await content_provider.get_tests(callback.from_user.id)
    variants = content_provider.variants_of(bundle.rows)
    if idx < 0 or idx >= len(variants):
        await callback.answer()
        return
    await state.update_data(test_filter_type="variant", test_filter_value=variants[idx], test_qty=None)
    await callback.message.edit_text("Сколько вопросов нужно?", reply_markup=quantity_kb())
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("tests:qty:"))  # type: ignore[call-arg]
async def qty_chosen(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    filter_type = data.get("test_filter_type") or "random"
    filter_value = data.get("test_filter_value")
    qty = (callback.data or "").split(":")[-1]
    await state.update_data(test_qty=qty)
    await callback.answer()

    chat_id = callback.message.chat.id
    await _start_test(
        state,
        user_id=callback.from_user.id,
        filter_type=str(filter_type),
        filter_value=filter_value if filter_value is None else str(filter_value),
        qty=str(qty),
        chat_id=chat_id,
        bot=callback.bot,
    )


async def _start_test(
    state: FSMContext,
    user_id: int,
    *,
    filter_type: str,
    filter_value: Optional[str],
    qty: str,
    chat_id: int,
    bot: Any,
) -> None:
    bundle = await content_provider.get_tests(user_id)
    if not bundle:
        await state.clear()
        await bot.send_message(
            chat_id=chat_id, text=bundle.empty_reason or "Тесты временно недоступны."
        )
        return

    qty_n: Optional[int] = None if qty == "all" else int(qty)

    rows = list(bundle.rows)
    if filter_type == "section":
        if not filter_value:
            rows = []
        else:
            # «Смешанные вопросы» — те, которым раздел не назначен
            key = "" if filter_value == UNSORTED_KEY else filter_value
            rows = [r for r in rows if _match_section(r.get("Раздел", ""), key)]
    elif filter_type == "variant":
        rows = [r for r in rows if (r.get("Вариант") or "") == (filter_value or "")]

    if not rows:
        await state.clear()
        await bot.send_message(chat_id=chat_id, text="По выбранным параметрам вопросов нет. Попробуй другой вариант.")
        return

    # Сначала то, что пора повторить, потом новое, потом уже освоенное.
    # Размер набора нужен, чтобы повторы не заняли всю тренировку.
    schedule = await get_schedule(user_id)
    rows = _sort_by_priority(rows, schedule, _utcnow().isoformat(), limit=qty_n)

    questions: List[Dict[str, Any]] = []
    for row in rows:
        question_text = row.get("Вопрос") or ""
        expected_raw = (row.get("Ответ") or "").strip()
        options = {i: _get_row_variant(row, i) for i in range(1, 6)}
        has_options = any(_is_real_option(options.get(i, "")) for i in range(1, 6))

        if filter_type == "part_b" and has_options:
            continue

        # Пропускаем битые вопросы (только инструкция, без содержания)
        qt = question_text.strip()
        if not has_options and not expected_raw and not qt:
            continue
        if not _is_usable_question(
            {
                "type": "A" if has_options else "B",
                "expected": expected_raw,
                "question_text": question_text,
                "options": options if has_options else {},
            }
        ):
            continue
        ql = qt.lower()
        if (("соответств" in ql or "соотнес" in ql or "последовательн" in ql or "хронологическ" in ql)
                and not re.search(r"[АБВГ]\)", qt) and len(qt) < 250):
            continue

        questions.append(
            {
                "type": "A" if has_options else "B",
                "expected": expected_raw,
                "question_text": question_text,
                "options": options if has_options else {},
            }
        )

        if qty_n is not None and len(questions) >= qty_n:
            break

    if not questions:
        await state.clear()
        await bot.send_message(
            chat_id=chat_id,
            text="По выбранным параметрам подходящих вопросов не нашлось. Попробуй другие настройки.",
        )
        return

    if bundle.fallback:
        await bot.send_message(chat_id=chat_id, text=FALLBACK_NOTE)

    await state.update_data(test_session={
        "questions": questions, "idx": 0, "wrong": [],
        "teacher_id": bundle.teacher_id, "subject": bundle.subject,
    })
    await _send_question(bot, chat_id, state, idx=0, user_id=user_id)


async def _send_question(bot: Any, chat_id: int, state: FSMContext, *, idx: int, user_id: int) -> None:
    data = await state.get_data()
    session = data.get("test_session") or {}
    questions: List[Dict[str, Any]] = session.get("questions") or []
    if idx >= len(questions):
        return

    q = questions[idx]
    total = len(questions)
    pos = idx + 1

    formatted = _format_question(q, pos, total)
    # Кнопка жалобы висит под вопросом: если он собрался криво, ответить
    # на него нечем, и сказать об этом надо здесь, а не после ответа
    await bot.send_message(
        chat_id=chat_id,
        text=formatted,
        reply_markup=question_report_kb(question_hash(q.get("question_text") or "")),
    )
    await state.set_state(TestFlow.waiting_answer)


# Кнопки меню и команды не считаются ответом на вопрос — иначе из теста
# нельзя выйти, а нажатие уходит в модель как ответ ученика
@router.message(
    TestFlow.waiting_answer,
    ~F.text.in_(ALL_MENU_BUTTONS),
    ~F.text.startswith("/"),
)
async def answer_test(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    session = data.get("test_session") or {}
    questions: List[Dict[str, Any]] = session.get("questions") or []
    idx = session.get("idx") or 0
    if idx >= len(questions):
        await state.clear()
        await message.answer("Ошибка сессии. Начни тест заново через меню.")
        return

    q = questions[idx]
    raw_text = (message.text or "").strip()
    if not raw_text or not re.search(r"[a-zA-Zа-яА-ЯёЁ0-9]", raw_text):
        await message.answer(_detect_hint(q))
        return

    # Отправляем как есть — LLM сам сравнит с эталоном
    student_answer = raw_text
    expected = q["expected"]

    # Для Part A: подставляем тексты вариантов вместо цифр
    if q["type"] == "A":
        options = q.get("options") or {}
        seen = set()
        digits = [int(ch) for ch in raw_text if ch.isdigit() and 1 <= int(ch) <= 5 and int(ch) not in seen and not seen.add(int(ch))]
        if digits:
            parts = []
            for d in digits:
                opt_text = (options.get(d) or "").strip()
                parts.append(f"{d}) {opt_text}" if opt_text else str(d))
            student_answer = "; ".join(parts)

    # Формируем контекст правильного ответа для LLM
    correct_for_llm = expected
    if q["type"] == "A" and expected:
        options = q.get("options") or {}
        digits = [int(ch) for ch in expected if ch.isdigit()]
        if len(digits) > 1:
            # Несколько правильных ответов (например "13" → варианты 1 и 3)
            parts = []
            for d in digits:
                opt_text = options.get(d, "")
                parts.append(f"{d}) {opt_text}" if opt_text else str(d))
            correct_for_llm = "; ".join(parts)
        elif len(digits) == 1:
            opt_text = options.get(digits[0], "")
            if opt_text:
                correct_for_llm = f"{digits[0]}) {opt_text}"
    elif q["type"] == "B":
        part_b_input_type = _get_part_b_input_type(q)
        if part_b_input_type:
            ok, normalized = normalize_part_b_input(raw_text, part_b_input_type)
            if not ok:
                await message.answer(_build_part_b_prompt(part_b_input_type))
                return
            student_answer = normalized
            if expected:
                correct_for_llm = _normalize_expected_part_b(expected)
        elif _detect_part_b_kind(q) == "year_open":
            ok, normalized_year = _normalize_year_input(raw_text)
            if not ok:
                await message.answer("Напиши год в виде 3–4 цифр, например: 1900.")
                return
            student_answer = normalized_year
            if expected:
                ok_expected_year, normalized_expected_year = _normalize_year_input(expected)
                correct_for_llm = normalized_expected_year if ok_expected_year else expected.strip()

    partial = _check_partial_credit(q, raw_text)
    if partial:
        llm_response = partial
    else:
        llm_response = await check_test_answer(
            question_text=q["question_text"],
            student_answer=student_answer,
            correct_answer=correct_for_llm,
        )
    verdict = verdict_of(llm_response)
    is_wrong = verdict == VERDICT_WRONG

    explain_button = explain_kb(idx) if (is_wrong and EXPLAIN_ERRORS_ENABLED) else None
    await message.answer(llm_response, reply_markup=explain_button)

    # Счётчики пишем сразу: досчитать их потом будет не из чего.
    # Неподтверждённый ответ не записываем вовсе — лучше пробел в статистике,
    # чем засчитанный как верный сбой проверки.
    if verdict != VERDICT_UNKNOWN:
        try:
            await record_answer(
                message.from_user.id,
                q.get("question_text", ""),
                is_correct=verdict == VERDICT_CORRECT,
                teacher_id=session.get("teacher_id"),
                subject=session.get("subject") or "",
                student_answer=raw_text,
            )
        except Exception:  # noqa: BLE001
            # Сбой учёта не должен останавливать тренировку на этом вопросе
            logger.exception("Не удалось записать ответ ученика %s", message.from_user.id)

    wrong = session.get("wrong") or []
    if is_wrong:
        wrong.append(idx)
        await add_mistake(message.from_user.id, q, "")
    await state.update_data(
        test_session={
            "questions": questions, "idx": idx, "wrong": wrong,
            "teacher_id": session.get("teacher_id"),
            "subject": session.get("subject"),
        }
    )

    await _advance_to_next_question(
        state,
        bot=message.bot,
        chat_id=message.chat.id,
        from_user_id=message.from_user.id,
        message_for_answers=message,
    )


async def _advance_to_next_question(
    state: FSMContext,
    *,
    bot: Any,
    chat_id: int,
    from_user_id: int,
    message_for_answers: Any,
) -> None:
    data = await state.get_data()
    session = data.get("test_session") or {}
    questions: List[Dict[str, Any]] = session.get("questions") or []
    idx = session.get("idx") or 0
    idx_next = idx + 1
    session["idx"] = idx_next
    await state.update_data(test_session=session)

    if idx_next >= len(questions):
        wrong = session.get("wrong") or []
        correct_count, total = _calc_result(questions, wrong)
        skipped = _skipped_positions(questions)
        await message_for_answers.answer(
            _result_screen(
                correct_count, total,
                [str(i + 1) for i in wrong if i not in skipped],
            ),
            reply_markup=test_result_kb(),
        )
        # Сессию закрываем, но вопросы оставляем: кнопки «Разобрать» уже
        # разосланы по чату, и после финиша они должны продолжать работать.
        await state.clear()
        await state.update_data(finished_test={"questions": questions})
        return

    await _send_question(bot, chat_id, state, idx=idx_next, user_id=from_user_id)


# ---------- Жалоба на вопрос ----------

def _find_in_session(session: Dict[str, Any], qhash: str) -> tuple:
    """Ищет вопрос по отпечатку. Возвращает (позиция, вопрос) или (None, None).

    Сессии может уже не быть — бот перезапускали, тест закончился. Тогда
    жалоба всё равно записывается: текст вопроса преподавателю подберётся
    по тому же отпечатку из его материалов.
    """
    for i, question in enumerate(session.get("questions") or []):
        if question_hash(question.get("question_text") or "") == qhash:
            return i, question
    return None, None


@router.callback_query(lambda c: c.data and c.data.startswith(f"{REPORT_PREFIX}:"))  # type: ignore[call-arg]
async def report_question(callback: CallbackQuery, state: FSMContext) -> None:
    """Кнопка «Что-то не так» и отмена. Меняем только кнопки — вопрос остаётся."""
    parts = (callback.data or "").split(":")
    await callback.answer()

    if len(parts) == 3 and parts[1] == "c":
        await callback.message.edit_reply_markup(
            reply_markup=question_report_kb(parts[2])
        )
        return

    if len(parts) != 2 or not parts[1]:
        return

    reasons = [(key, label) for key, label in reports.REASON_LABELS.items()]
    await callback.message.edit_reply_markup(
        reply_markup=report_reasons_kb(parts[1], reasons)
    )


async def _notify_teacher_hidden(bot: Any, teacher_id: int, preview: str) -> None:
    """Сообщение преподавателю, когда вопрос скрылся сам.

    Молчать нельзя: вопросы пропадали бы у учеников, а тренажёр молча худел.
    """
    try:
        await bot.send_message(
            chat_id=teacher_id,
            text=(
                "⚠️ Вопрос скрыт у учеников\n\n"
                f"«{short_text(preview)}»\n"
                f"Отметили {reports.HIDE_THRESHOLD}: вопрос собран неверно"
            ),
            reply_markup=reported_open_kb(),
        )
    except Exception:  # noqa: BLE001
        # Преподаватель мог не начать диалог с ботом — жалоба всё равно записана
        logger.warning("Не удалось уведомить преподавателя %s", teacher_id)


@router.callback_query(lambda c: c.data and c.data.startswith(f"{REASON_PREFIX}:"))  # type: ignore[call-arg]
async def report_reason_chosen(callback: CallbackQuery, state: FSMContext) -> None:
    parts = (callback.data or "").split(":")
    await callback.answer()
    if len(parts) != 3 or not parts[1]:
        return

    qhash = parts[1]
    reason = parts[2]
    if not reports.is_valid_reason(reason):
        return

    await callback.message.edit_reply_markup(reply_markup=None)

    # Преподавателя берём из привязки ученика, а не из сессии: жалоба
    # может прийти из старого сообщения, когда сессии давно нет
    user = await get_user(callback.from_user.id)
    teacher_id = content_provider.resolve_teacher_id(user) if user else None
    if not teacher_id:
        # Без преподавателя жалобу некому показывать
        await callback.message.answer("Спасибо, отметил.")
        return

    subject = (user.current_subject if user else "") or ""

    data = await state.get_data()
    session = data.get("test_session") or {}
    pos, question = _find_in_session(session, qhash)
    if question is None:
        pos, question = _find_in_session(data.get("finished_test") or {}, qhash)
    text = (question or {}).get("question_text") or ""

    collected = await add_question_report(
        teacher_id, subject, qhash, callback.from_user.id, reason, text
    )

    status = await get_question_status(teacher_id, qhash)
    if reports.should_hide(collected, status):
        await set_question_status(teacher_id, subject, qhash, reports.STATUS_HIDDEN)
        await _notify_teacher_hidden(callback.bot, teacher_id, text)

    if not reports.hides_for_student(reason):
        # «Не понял» — вопрос остаётся, отвечать всё равно нужно
        await callback.message.answer(
            "Отметил. Преподаватель увидит, что вопрос оказался сложным."
        )
        return

    await callback.message.answer(
        "Спасибо. Этот вопрос тебе больше не попадётся,\n"
        "преподаватель его посмотрит."
    )

    if question is None or not data.get("test_session"):
        return

    # Сломанный вопрос из счёта убираем: отвечать на него нечего, а если
    # ответ уже был — считать его ошибкой ученика нечестно
    question["skipped"] = True
    await state.update_data(test_session=session)

    # Дальше двигаем, только если пожаловались на текущий вопрос. Кнопки
    # прошлых вопросов остаются в чате, и по ним жмут — от такого нажатия
    # тест не должен перескакивать через вопрос на экране.
    if pos == session.get("idx"):
        await _advance_to_next_question(
            state,
            bot=callback.bot,
            chat_id=callback.message.chat.id,
            from_user_id=callback.from_user.id,
            message_for_answers=callback.message,
        )


@router.callback_query(lambda c: c.data and c.data.startswith("tests:explain:"))  # type: ignore[call-arg]
async def explain_mistake(callback: CallbackQuery, state: FSMContext) -> None:
    """Разбор ошибки: почему верен ответ преподавателя."""
    if not EXPLAIN_ERRORS_ENABLED:
        await callback.answer("Разбор временно отключён.", show_alert=True)
        return

    try:
        idx = int((callback.data or "").split(":")[-1])
    except ValueError:
        await callback.answer()
        return

    data = await state.get_data()
    # Тест может быть ещё в процессе или уже завершён — кнопка работает в обоих
    session = data.get("test_session") or data.get("finished_test") or {}
    questions: List[Dict[str, Any]] = session.get("questions") or []
    if idx < 0 or idx >= len(questions):
        await callback.answer("Этот вопрос уже недоступен.", show_alert=True)
        return

    q = questions[idx]
    await callback.answer()

    # Кнопку убираем сразу: повторное нажатие — лишнее обращение к модели
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:  # noqa: BLE001
        pass

    waiting = await callback.message.answer("⏳ Готовлю объяснение…")
    try:
        explanation = await explain_answer(
            question_text=q.get("question_text", ""),
            expected=q.get("expected") or "",
            options=q.get("options") or {},
        )
    except Exception:  # noqa: BLE001
        logger.exception("Ошибка при разборе вопроса")
        explanation = None

    try:
        await waiting.delete()
    except Exception:  # noqa: BLE001
        pass

    if not explanation:
        # Молчим осознанно: объяснения либо нет, либо оно не прошло проверку
        # на приписанные факты. Лучше ничего, чем неверное.
        await callback.message.answer(
            "Подробного разбора для этого вопроса нет — ориентируйся на правильный ответ выше."
        )
        return

    await callback.message.answer(explanation)


@router.callback_query(lambda c: c.data == "test:restart")  # type: ignore[call-arg]
async def test_restart(callback: CallbackQuery, state: FSMContext) -> None:
    # MVP: просто пересоздаём вопросы из уже выбранного набора, если он есть в FSM.
    await state.clear()
    await callback.message.edit_text("Начни тест заново через меню.", reply_markup=tests_root_kb())
    await callback.answer()


@router.callback_query(lambda c: c.data == "test:other")  # type: ignore[call-arg]
async def test_other(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text("Режим: Тесты", reply_markup=tests_root_kb())
    await callback.answer()

