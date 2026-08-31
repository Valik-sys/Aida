"""Журнал занятий: из записей об ответах — таблица для преподавателя.

Формат просили пятеро преподавателей из пяти и назвали дословно: дата,
тема, сколько решил, процент, перевод в десятибалльную. И просили именно
файлом, а не экраном в телеграме: с файлом можно работать — отсортировать,
показать родителю, положить в свою таблицу.

Логика здесь чистая: ни бота, ни базы. На вход — записи журнала и строки
контента, на выход — строки таблицы.

**Тема берётся из самих вопросов, а не из журнала.** В журнале лежит
отпечаток вопроса, а место (раздел или тема) живёт в строке контента —
так тема у старых ответов остаётся верной, даже если преподаватель потом
переложил файл.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

from services import sections as sections_lib


# Перевод процента в десятибалльную.
#
# ВНИМАНИЕ: шкала поставлена по здравому смыслу и ждёт подтверждения от
# преподавателя. Это единственное место, где её надо править, — везде
# дальше используется только `ten_point_mark`.
TEN_POINT_SCALE = (
    (10, 1), (20, 2), (30, 3), (40, 4), (50, 5),
    (60, 6), (70, 7), (80, 8), (90, 9), (101, 10),
)

# Ответ на вопрос, которого больше нет в тренажёре. Выбрасывать такие
# записи нельзя: ученик на них отвечал, и в проценте они участвовали.
UNKNOWN_PLACE = "Вне тренажёра"


def ten_point_mark(percent: int) -> int:
    for bound, mark in TEN_POINT_SCALE:
        if percent < bound:
            return mark
    return TEN_POINT_SCALE[-1][1]


@dataclass
class JournalRow:
    student: str
    date: str
    place: str
    answered: int
    correct: int

    @property
    def percent(self) -> int:
        return round(100 * self.correct / self.answered) if self.answered else 0

    @property
    def mark(self) -> int:
        return ten_point_mark(self.percent)


def place_by_question(rows: Iterable[Dict[str, str]], hasher) -> Dict[str, str]:
    """Карта «отпечаток вопроса → ключ места» по строкам контента."""
    out: Dict[str, str] = {}
    for row in rows:
        question = (row.get("Вопрос") or "").strip()
        if not question:
            continue
        out[hasher(question)] = (row.get("Раздел") or "").strip()
    return out


def _place_title(
    subject: str,
    key: Optional[str],
    custom_sections: Optional[Dict[str, str]],
    custom_topics: Optional[Dict[str, str]],
) -> str:
    if key is None:
        return UNKNOWN_PLACE
    return sections_lib.place_title(subject, key, custom_sections, custom_topics)


def build(
    entries: Iterable[Dict[str, Any]],
    *,
    subject: str,
    places: Dict[str, str],
    names: Dict[int, str],
    custom_sections: Optional[Dict[str, str]] = None,
    custom_topics: Optional[Dict[str, str]] = None,
) -> List[JournalRow]:
    """Строки журнала: одна на связку «ученик — день — тема».

    Так преподаватель видит занятие целиком: в один день по одной теме
    ученик отвечал двадцать раз — это одна строка с процентом, а не двадцать
    строк, между которыми нечего выбирать.
    """
    buckets: Dict[tuple, List[int]] = {}

    for entry in entries:
        user_id = entry.get("user_id")
        answered_at = str(entry.get("answered_at") or "")
        if not answered_at:
            continue
        day = answered_at[:10]
        key = places.get(str(entry.get("question_hash")))
        title = _place_title(subject, key, custom_sections, custom_topics)

        slot = buckets.setdefault((user_id, day, title), [0, 0])
        slot[0] += 1
        slot[1] += 1 if entry.get("is_correct") else 0

    out = [
        JournalRow(
            student=names.get(user_id) or str(user_id),
            date=day,
            place=title,
            answered=answered,
            correct=correct,
        )
        for (user_id, day, title), (answered, correct) in buckets.items()
    ]
    # Сначала ученик, потом дни по возрастанию: журнал читают по ученику
    out.sort(key=lambda r: (r.student, r.date, r.place))
    return out


HEADERS_TEACHER = ["Ученик", "Дата", "Тема", "Решено", "Верно", "Процент", "Балл"]
HEADERS_STUDENT = ["Дата", "Тема", "Решено", "Верно", "Процент", "Балл"]


def to_csv(rows: List[JournalRow], with_student: bool = True) -> bytes:
    """Таблица в CSV для Excel.

    Точка с запятой и метка кодировки в начале файла — иначе Excel
    на русской раскладке сваливает всё в одну колонку и показывает
    кириллицу крокозябрами. Это не придирка к красоте: файл, который
    открывается нечитаемым, преподаватель просто выбросит.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";", lineterminator="\r\n")
    writer.writerow(HEADERS_TEACHER if with_student else HEADERS_STUDENT)

    for row in rows:
        line = [row.date, row.place, row.answered, row.correct, row.percent, row.mark]
        writer.writerow(([row.student] + line) if with_student else line)

    return buffer.getvalue().encode("utf-8-sig")


def filename(prefix: str, today: Optional[dt.date] = None) -> str:
    day = (today or dt.date.today()).isoformat()
    return f"{prefix}_{day}.csv"
