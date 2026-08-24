from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import gspread

import config
from services import storage
from subjects import DEFAULT_SUBJECT

# Снимок общего контента предмета. Пока история — единственный предмет
# с наполнением, поэтому базовый слой один; когда появится второй предмет
# со своими таблицами, кэш станет пер-предметным.
_CACHE_PATH = storage.base_sheets_path(DEFAULT_SUBJECT)


logger = logging.getLogger(__name__)


def _norm_cell(v: Any) -> str:
    if v is None:
        return ""
    return str(v).strip()


def _open_worksheet(ss: gspread.Spreadsheet, title: str) -> gspread.Worksheet:
    if not title:
        return ss.sheet1
    try:
        return ss.worksheet(title)
    except Exception:  # noqa: BLE001
        return ss.sheet1


# ---------- Вопросы по темам как часть общего набора ----------
#
# Общий контент приехал из двух листов таблицы: обычные вопросы размечены
# разделами, а отдельный лист «Вопросы по темам» — темами. Наборы почти
# не пересекаются, и второй ученику не показывался вовсе: его читал только
# режим «По темам».
#
# Поэтому при загрузке они складываются в один набор. Разметка тем при этом
# попадает в то же поле «Раздел», что и у преподавателя, — темы у общих
# вопросов появляются сами собой, отдельной ветки в коде не нужно.
#
# Изоляции это не касается: чей контент показать — по-прежнему решает
# services/content_provider.py, и вопросы преподавателя с общими не мешаются.

_TOPIC_COLUMNS = {
    "Часть": "Часть",
    "№": "№",
    "Вопрос": "Вопрос",
    "Ответ": "Ответ",
}


def _fingerprint(question: str) -> str:
    return " ".join((question or "").split()).casefold()[:120]


def topic_rows_as_tests(
    topic_rows: List[Dict[str, str]],
    known: Optional[List[Dict[str, str]]] = None,
) -> List[Dict[str, str]]:
    """Приводит вопросы по темам к схеме строки тренажёра.

    Отличий три: тема лежит в «Тема_код», варианты названы «Вар1» вместо
    «Вар.1», а поля «Вариант» нет вовсе — эти вопросы не из билета, и в
    тренировке по варианту им делать нечего.

    Вопросы без темы, без текста или без ответа пропускаем: тот же фильтр
    качества, что и у билетов преподавателя. Совпадающие с обычным набором
    тоже — иначе один и тот же вопрос встретится ученику дважды.
    """
    seen = {_fingerprint(str(r.get("Вопрос") or "")) for r in (known or [])}
    seen.discard("")

    out: List[Dict[str, str]] = []
    for row in topic_rows or []:
        topic = str(row.get("Тема_код") or "").strip()
        question = str(row.get("Вопрос") or "").strip()
        answer = str(row.get("Ответ") or "").strip()
        if not topic or not question or not answer:
            continue

        mark = _fingerprint(question)
        if mark in seen:
            continue
        seen.add(mark)

        converted = {name: str(row.get(src) or "").strip() for src, name in _TOPIC_COLUMNS.items()}
        # «Б» вместо «В» — опечатка разметки: часть в билете одна и та же
        if converted.get("Часть") == "Б":
            converted["Часть"] = "В"
        for i in range(1, 6):
            converted[f"Вар.{i}"] = str(row.get(f"Вар{i}") or "").strip()
        converted["Вариант"] = ""
        converted["Раздел"] = topic
        out.append(converted)

    return out


@dataclass
class SheetsCache:
    tests_rows: List[Dict[str, str]]
    topic_tests_rows: List[Dict[str, str]]
    flash_dates: List[Dict[str, str]]
    flash_concepts: List[Dict[str, str]]
    flash_persons: List[Dict[str, str]]
    loaded_at: Optional[float] = None
    _combined: Optional[List[Dict[str, str]]] = field(default=None, repr=False)
    _combined_from: Optional[tuple] = field(default=None, repr=False)

    @property
    def base_tests_rows(self) -> List[Dict[str, str]]:
        """Весь общий набор вопросов: обычные плюс размеченные темами.

        Единственное, что должен читать общий слой тренировки. Прямое чтение
        `tests_rows` отдаёт половину набора и теряет темы.

        Склейка считается один раз на набор: она меняется только при `/update`,
        а к этому свойству обращаются на каждое нажатие кнопки. Готовое
        отдаётся, только если исходные наборы те же самые — иначе подменённый
        набор молча отдавал бы прежние вопросы.
        """
        signature = (
            id(self.tests_rows), len(self.tests_rows or []),
            id(self.topic_tests_rows), len(self.topic_tests_rows or []),
        )
        if self._combined is None or self._combined_from != signature:
            self._combined = list(self.tests_rows or []) + topic_rows_as_tests(
                self.topic_tests_rows, self.tests_rows
            )
            self._combined_from = signature
        return self._combined

    async def load_all(self) -> None:
        def _load_sync() -> Dict[str, Any]:
            res: Dict[str, Any] = {
                "tests_rows": [],
                "topic_tests_rows": [],
                "flash_dates": [],
                "flash_concepts": [],
                "flash_persons": [],
            }

            if not (config.TESTS_SHEET_ID or config.FLASHCARDS_SHEET_ID):
                logger.warning("Sheets IDs не заданы.")
                return res

            creds_path = config.GOOGLE_CREDENTIALS_PATH
            if not os.path.exists(creds_path):
                logger.error("Google credentials file not found: %s", creds_path)
                return res

            gc = gspread.service_account(filename=creds_path)

            # Tests
            if config.TESTS_SHEET_ID:
                try:
                    ss = gc.open_by_key(config.TESTS_SHEET_ID)
                    ws = _open_worksheet(ss, config.TESTS_WORKSHEET_NAME)
                    res["tests_rows"] = [
                        {k: _norm_cell(v) for k, v in row.items()}
                        for row in ws.get_all_records()
                    ]
                except Exception:  # noqa: BLE001
                    logger.exception("Не удалось загрузить таблицу тестов.")

                try:
                    ss_tests = gc.open_by_key(config.TESTS_SHEET_ID)
                    ws_topics = ss_tests.worksheet(config.TOPIC_TESTS_WORKSHEET_NAME)
                    res["topic_tests_rows"] = [
                        {k: _norm_cell(v) for k, v in row.items()}
                        for row in ws_topics.get_all_records()
                    ]
                except Exception:  # noqa: BLE001
                    logger.exception("Не удалось загрузить лист «%s».", config.TOPIC_TESTS_WORKSHEET_NAME)

            # Flashcards
            if config.FLASHCARDS_SHEET_ID:
                try:
                    ss = gc.open_by_key(config.FLASHCARDS_SHEET_ID)
                    ws_dates = ss.worksheet(config.FLASH_DATES_WORKSHEET_NAME)
                    ws_concepts = ss.worksheet(config.FLASH_CONCEPTS_WORKSHEET_NAME)
                    ws_persons = ss.worksheet(config.FLASH_PERSONS_WORKSHEET_NAME)

                    res["flash_dates"] = [
                        {k: _norm_cell(v) for k, v in row.items()} for row in ws_dates.get_all_records()
                    ]
                    res["flash_concepts"] = [
                        {k: _norm_cell(v) for k, v in row.items()}
                        for row in ws_concepts.get_all_records()
                    ]
                    res["flash_persons"] = [
                        {k: _norm_cell(v) for k, v in row.items()}
                        for row in ws_persons.get_all_records()
                    ]
                except Exception:  # noqa: BLE001
                    logger.exception("Не удалось загрузить таблицы карточек.")

            return res

        loaded = await asyncio.to_thread(_load_sync)
        self.tests_rows = loaded["tests_rows"]
        self.topic_tests_rows = loaded["topic_tests_rows"]
        self._combined = None
        self.flash_dates = loaded["flash_dates"]
        self.flash_concepts = loaded["flash_concepts"]
        self.flash_persons = loaded["flash_persons"]
        self.loaded_at = asyncio.get_event_loop().time()

        logger.info(
            "Sheets cache loaded: tests=%s topic_tests=%s dates=%s concepts=%s persons=%s",
            len(self.tests_rows),
            len(self.topic_tests_rows),
            len(self.flash_dates),
            len(self.flash_concepts),
            len(self.flash_persons),
        )
        self._save_to_disk()

    def _save_to_disk(self) -> None:
        try:
            _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "tests_rows": self.tests_rows,
                "topic_tests_rows": self.topic_tests_rows,
                "flash_dates": self.flash_dates,
                "flash_concepts": self.flash_concepts,
                "flash_persons": self.flash_persons,
            }
            _CACHE_PATH.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            logger.info("Sheets cache saved to disk: %s", _CACHE_PATH)
        except Exception:
            logger.exception("Не удалось сохранить sheets cache на диск")

    def load_from_disk(self) -> bool:
        """Загружает кэш с диска. Возвращает True если успешно."""
        if not _CACHE_PATH.exists():
            return False
        try:
            data = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
            self.tests_rows = data.get("tests_rows", [])
            self.topic_tests_rows = data.get("topic_tests_rows", [])
            self._combined = None
            self.flash_dates = data.get("flash_dates", [])
            self.flash_concepts = data.get("flash_concepts", [])
            self.flash_persons = data.get("flash_persons", [])
            logger.info(
                "Sheets cache loaded from disk: tests=%s topic_tests=%s dates=%s concepts=%s persons=%s",
                len(self.tests_rows), len(self.topic_tests_rows),
                len(self.flash_dates), len(self.flash_concepts), len(self.flash_persons),
            )
            return True
        except Exception:
            logger.exception("Не удалось прочитать sheets cache с диска")
            return False


sheets_cache = SheetsCache(
    tests_rows=[],
    topic_tests_rows=[],
    flash_dates=[],
    flash_concepts=[],
    flash_persons=[],
)


def search_flashcards(query: str, max_results: int = 10) -> str:
    """Simple keyword search across all flashcard tables. Returns formatted context string."""
    query_lower = query.lower()
    tokens = [t for t in query_lower.split() if len(t) >= 3]
    if not tokens:
        return ""
    results: list[str] = []

    for card in sheets_cache.flash_dates or []:
        text = f"{card.get('Дата', '')} {card.get('Событие', '')}".lower()
        matches = sum(1 for t in tokens if t in text)
        if matches >= 2:
            results.append((matches, f"[Дата] {card.get('Дата', '')} — {card.get('Событие', '')}"))

    for card in sheets_cache.flash_concepts or []:
        text = f"{card.get('Понятие', '')} {card.get('Определение', '')}".lower()
        matches = sum(1 for t in tokens if t in text)
        if matches >= 2:
            results.append((matches, f"[Понятие] {card.get('Понятие', '')} — {card.get('Определение', '')}"))

    for card in sheets_cache.flash_persons or []:
        text = f"{card.get('Личность', '')} {card.get('Роль в истории', '')}".lower()
        matches = sum(1 for t in tokens if t in text)
        if matches >= 2:
            results.append((matches, f"[Личность] {card.get('Личность', '')} — {card.get('Роль в истории', '')}"))

    results.sort(key=lambda x: x[0], reverse=True)
    return "\n".join(r[1] for r in results[:max_results])


def get_topic_questions(tema_kod: str) -> List[Dict[str, Any]]:
    """Возвращает вопросы по коду темы (например '1_1', '2_3')."""
    questions: List[Dict[str, Any]] = []
    for row in sheets_cache.topic_tests_rows:
        if (row.get("Тема_код") or "").strip() != tema_kod:
            continue
        question_text = row.get("Вопрос") or ""
        expected = (row.get("Ответ") or "").strip()
        options = {i: (row.get(f"Вар{i}") or "").strip() for i in range(1, 6)}
        has_options = any(options.get(i, "") for i in range(1, 6))
        questions.append({
            "type": "A" if has_options else "B",
            "expected": expected,
            "question_text": question_text,
            "options": options if has_options else {},
        })
    return questions

