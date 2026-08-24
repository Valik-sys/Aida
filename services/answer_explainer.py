"""Объяснение ошибки от ответа преподавателя, без поиска по материалам.

Почему так, а не через RAG:

  • работает у любого предмета, даже когда материалов нет вообще —
    ответ преподавателя есть всегда, иначе вопрос не прошёл бы фильтр;
  • модель не может ошибиться в самом ответе, он дан ей готовым;
  • дёшево: одно короткое обращение к модели без поиска по векторной базе.

Остаётся риск, что модель присочинит обоснование. Он давится не доверием
к инструкции, а машинной проверкой: в объяснении не должно быть дат, имён
и названий, которых нет в вопросе, вариантах или ответе преподавателя.
Тот же приём, что с посимвольной сверкой в парсере билетов.
"""

from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional

from openai import AsyncOpenAI

import config


logger = logging.getLogger(__name__)

_client: Optional[AsyncOpenAI] = None


SYSTEM_PROMPT = (
    "Ты объясняешь ученику, почему правильный ответ верен.\n\n"
    "ГЛАВНОЕ ПРАВИЛО: правильный ответ дал преподаватель — это истина. "
    "Не оспаривай его и не предлагай свой вариант.\n\n"
    "ЗАПРЕЩЕНО добавлять даты, имена, названия и любые факты, которых нет "
    "в тексте вопроса, вариантах или ответе. Если добавить нечего — просто "
    "поясни своими словами, что означает правильный ответ и почему он "
    "подходит к вопросу.\n\n"
    "Формат: 2–3 предложения, обращайся на «ты», без вступлений и воды."
)

USER_TEMPLATE = (
    "Вопрос:\n{question}\n\n"
    "{options_block}"
    "Правильный ответ: {answer}\n\n"
    "Объясни коротко, почему этот ответ верный."
)


def _get_client() -> AsyncOpenAI:
    global _client  # noqa: PLW0603
    if _client is None:
        _client = AsyncOpenAI(api_key=config.OPENAI_API_KEY)
    return _client


# ---------- Проверка на приписанные факты ----------

_YEAR_RE = re.compile(r"\b(?:1[0-9]{3}|20[0-9]{2})\b")
_ROMAN_RE = re.compile(r"\b[IVXLC]{1,6}\b")
# Имя собственное: заглавная и строчные после неё («Люблинская»)
_PROPER_RE = re.compile(r"[А-ЯЁA-Z][а-яёa-z]{3,}")
# Аббревиатура целиком заглавными («СССР», «БССР», «ВКЛ»)
_ACRONYM_RE = re.compile(r"[А-ЯЁ]{2,}")
_SENTENCE_SPLIT_RE = re.compile(r"[.!?]\s+")

# Русский склоняет: «Речью» против «Речи», «Люблинская» против «Люблинской».
# Сравниваем по основе слова, отбрасывая окончание. Список от длинных
# к коротким, иначе «ами» никогда не сработает — раньше отвалится «и».
_ENDINGS = (
    "ами", "ями", "ого", "его", "ому", "ему", "ыми", "ими", "ых", "их",
    "ой", "ей", "ай", "ий", "ый", "ом", "ем", "им", "ым", "ах", "ях",
    "ам", "ям", "ов", "ев", "ые", "ие", "ая", "яя", "ое", "ее", "ии",
    "ию", "ью", "ья", "ье", "ую", "юю", "ою", "ею", "ла", "ло", "ли",
    "а", "е", "и", "о", "у", "ы", "ю", "я", "ь", "й",
)
_MIN_STEM = 3



def _fold(word: str) -> str:
    return word.lower().replace("ё", "е")


def _stem(word: str) -> str:
    """Грубая основа слова: отбрасывает типичное русское окончание."""
    folded = _fold(word)
    for ending in _ENDINGS:
        if folded.endswith(ending) and len(folded) - len(ending) >= _MIN_STEM:
            return folded[: -len(ending)]
    return folded


def _source_stems(source: str) -> set:
    return {_stem(w) for w in re.findall(r"[А-Яа-яЁёA-Za-z]+", source)}


def find_unsupported_facts(explanation: str, source: str) -> List[str]:
    """Возвращает фрагменты объяснения, которых нет в исходнике.

    Проверяются годы, римские века, имена собственные и аббревиатуры.
    Сравнение по основе слова, чтобы склонения не давали ложных срабатываний.
    """
    if not explanation.strip():
        return []

    source_years = set(_YEAR_RE.findall(source))
    source_roman = {r.upper() for r in _ROMAN_RE.findall(source)}
    source_stems = _source_stems(source)

    suspicious: List[str] = []

    for year in _YEAR_RE.findall(explanation):
        if year not in source_years:
            suspicious.append(year)

    for sentence in _SENTENCE_SPLIT_RE.split(explanation):
        for position, token in enumerate(sentence.split()):
            clean = token.strip("«»\"'(),;:.!?—–-")
            if not clean:
                continue

            if _ROMAN_RE.fullmatch(clean):
                if clean.upper() not in source_roman:
                    suspicious.append(clean)
                continue

            if _ACRONYM_RE.fullmatch(clean):
                if _stem(clean) not in source_stems:
                    suspicious.append(clean)
                continue

            # Слово с заглавной в начале предложения — не признак имени
            # собственного: там так пишут по правилам. Годы, века и
            # аббревиатуры выше проверяются и в этой позиции, они однозначны.
            if position == 0:
                continue

            if _PROPER_RE.fullmatch(clean) and _stem(clean) not in source_stems:
                suspicious.append(clean)

    seen = set()
    return [x for x in suspicious if not (x in seen or seen.add(x))]


def build_source_text(
    question_text: str,
    options: Optional[Dict[int, str]],
    expected: str,
) -> str:
    """Всё, на что объяснению разрешено опираться."""
    parts = [question_text or ""]
    for value in (options or {}).values():
        if value:
            parts.append(str(value))
    if expected:
        parts.append(str(expected))
    return "\n".join(parts)


# ---------- Основная функция ----------

async def explain_answer(
    question_text: str,
    expected: str,
    options: Optional[Dict[int, str]] = None,
) -> Optional[str]:
    """Короткое объяснение правильного ответа.

    Возвращает None, если объяснить нечем или модель приписала факты,
    которых нет в вопросе, — лучше промолчать, чем научить неверному.
    """
    if not (question_text or "").strip() or not (expected or "").strip():
        return None

    options_block = ""
    if options:
        listed = "\n".join(
            f"{num}) {text}" for num, text in sorted(options.items()) if str(text).strip()
        )
        if listed:
            options_block = f"Варианты:\n{listed}\n\n"

    user_prompt = USER_TEMPLATE.format(
        question=question_text.strip(),
        options_block=options_block,
        answer=expected.strip(),
    )

    try:
        resp = await _get_client().chat.completions.create(
            model=config.LLM_MODEL_MINI,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.2,
            max_tokens=300,
        )
        explanation = (resp.choices[0].message.content or "").strip()
    except Exception:  # noqa: BLE001
        logger.exception("Ошибка обращения к модели при объяснении ответа")
        return None

    if not explanation:
        return None

    source = build_source_text(question_text, options, expected)
    unsupported = find_unsupported_facts(explanation, source)
    if unsupported:
        # Считаем, сколько раз срабатывает: по этому видно, стоит ли
        # ужесточать инструкцию или ослаблять проверку
        logger.warning(
            "Объяснение отброшено — факты вне вопроса: %s", ", ".join(unsupported[:5])
        )
        return None

    return explanation
