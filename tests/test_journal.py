"""Журнал занятий файлом.

Формат просили пятеро преподавателей из пяти и назвали дословно: дата,
тема, сколько решил, процент, перевод в десятибалльную. Просили именно
файлом — экран в телеграме им не подходит.

Здесь проверяется две вещи: что строки собираются верно и что файл
открывается читаемым. Второе не придирка: CSV с кириллицей, открытый
Excel-ем в крокозябрах, преподаватель просто выбросит.
"""

import csv
import io
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services import journal  # noqa: E402

SUBJECT = "history"


def _hash(text: str) -> str:
    """Отпечаток вопроса — тот же приём, что в базе."""
    import hashlib

    return hashlib.md5(text.encode()).hexdigest()[:16]


def _entry(user_id, question, correct, when):
    return {
        "user_id": user_id,
        "question_hash": _hash(question),
        "is_correct": correct,
        "answered_at": when,
    }


CONTENT = [
    {"Вопрос": "Кто основал город?", "Раздел": "6_2"},
    {"Вопрос": "В каком году это было?", "Раздел": "6_2"},
    {"Вопрос": "Что такое чинш?", "Раздел": "3"},
]


class TestMark:
    """Перевод процента в десятибалльную."""

    def test_edges(self):
        assert journal.ten_point_mark(0) == 1
        assert journal.ten_point_mark(100) == 10

    def test_ladder_is_monotone(self):
        marks = [journal.ten_point_mark(p) for p in range(0, 101)]
        assert marks == sorted(marks)

    def test_known_points(self):
        assert journal.ten_point_mark(55) == 6
        assert journal.ten_point_mark(85) == 9


class TestBuild:
    def _build(self, entries, names=None):
        return journal.build(
            entries,
            subject=SUBJECT,
            places=journal.place_by_question(CONTENT, _hash),
            names=names or {1: "Иванов Иван"},
        )

    def test_one_row_per_day_and_topic(self):
        """Двадцать ответов по одной теме за день — это одно занятие."""
        entries = [
            _entry(1, "Кто основал город?", True, "2026-08-25T10:00:00"),
            _entry(1, "В каком году это было?", False, "2026-08-25T10:05:00"),
        ]
        rows = self._build(entries)

        assert len(rows) == 1
        assert rows[0].answered == 2 and rows[0].correct == 1

    def test_percent_and_mark(self):
        entries = [
            _entry(1, "Кто основал город?", True, "2026-08-25T10:00:00"),
            _entry(1, "В каком году это было?", True, "2026-08-25T10:05:00"),
        ]
        rows = self._build(entries)

        assert rows[0].percent == 100
        assert rows[0].mark == 10

    def test_topic_name_comes_from_content(self):
        rows = self._build([_entry(1, "Кто основал город?", True, "2026-08-25T10:00:00")])
        assert "1917" in rows[0].place  # раздел 6 в названии темы

    def test_different_days_are_different_lessons(self):
        entries = [
            _entry(1, "Кто основал город?", True, "2026-08-25T10:00:00"),
            _entry(1, "Кто основал город?", True, "2026-08-26T10:00:00"),
        ]
        assert len(self._build(entries)) == 2

    def test_different_topics_are_different_rows(self):
        entries = [
            _entry(1, "Кто основал город?", True, "2026-08-25T10:00:00"),
            _entry(1, "Что такое чинш?", True, "2026-08-25T11:00:00"),
        ]
        assert len(self._build(entries)) == 2

    def test_students_do_not_mix(self):
        entries = [
            _entry(1, "Кто основал город?", True, "2026-08-25T10:00:00"),
            _entry(2, "Кто основал город?", False, "2026-08-25T10:00:00"),
        ]
        rows = self._build(entries, names={1: "Иванов", 2: "Петрова"})

        assert {r.student for r in rows} == {"Иванов", "Петрова"}
        assert len(rows) == 2

    def test_answer_to_removed_question_is_kept(self):
        """Вопроса больше нет в тренажёре, но ученик на него отвечал.

        Выбросить такую запись — соврать в проценте за тот день.
        """
        rows = self._build([_entry(1, "Вопрос из удалённого файла", False,
                                   "2026-08-25T10:00:00")])

        assert len(rows) == 1
        assert rows[0].place == journal.UNKNOWN_PLACE

    def test_rows_sorted_by_student_then_date(self):
        entries = [
            _entry(2, "Кто основал город?", True, "2026-08-26T10:00:00"),
            _entry(1, "Кто основал город?", True, "2026-08-26T10:00:00"),
            _entry(1, "Кто основал город?", True, "2026-08-25T10:00:00"),
        ]
        rows = self._build(entries, names={1: "Иванов", 2: "Петрова"})

        assert [(r.student, r.date) for r in rows] == [
            ("Иванов", "2026-08-25"), ("Иванов", "2026-08-26"), ("Петрова", "2026-08-26"),
        ]

    def test_entry_without_date_is_skipped(self):
        assert self._build([_entry(1, "Кто основал город?", True, "")]) == []


class TestCsv:
    def _rows(self):
        return [journal.JournalRow("Иванов Иван", "2026-08-25", "6. 1917–1945", 10, 7)]

    def test_opens_readable_in_excel(self):
        """Метка кодировки и точка с запятой — иначе Excel всё портит."""
        payload = journal.to_csv(self._rows())

        assert payload.startswith(b"\xef\xbb\xbf"), "нет метки UTF-8 — будут крокозябры"
        assert b";" in payload, "запятая вместо точки с запятой — всё в одной колонке"

    def test_headers_are_as_asked(self):
        text = journal.to_csv(self._rows()).decode("utf-8-sig")
        header = next(csv.reader(io.StringIO(text), delimiter=";"))

        assert header == ["Ученик", "Дата", "Тема", "Решено", "Верно", "Процент", "Балл"]

    def test_values_land_in_their_columns(self):
        text = journal.to_csv(self._rows()).decode("utf-8-sig")
        rows = list(csv.reader(io.StringIO(text), delimiter=";"))

        assert rows[1] == ["Иванов Иван", "2026-08-25", "6. 1917–1945", "10", "7", "70", "8"]

    def test_student_file_has_no_name_column(self):
        text = journal.to_csv(self._rows(), with_student=False).decode("utf-8-sig")
        header = next(csv.reader(io.StringIO(text), delimiter=";"))

        assert header[0] == "Дата"
        assert "Ученик" not in header

    def test_filename_carries_the_date(self):
        import datetime as dt

        name = journal.filename("Журнал класса", dt.date(2026, 8, 25))
        assert name == "Журнал класса_2026-08-25.csv"
