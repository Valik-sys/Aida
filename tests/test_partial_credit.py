"""
Tests for partial-credit logic and Part B classification:
- _check_partial_credit (type A and type B multiple)
- detect_part_b_type
- normalize_part_b_input
- _detect_part_b_kind
"""
import pytest
from bot.handlers.tests import (
    _check_partial_credit,
    _detect_part_b_kind,
    detect_part_b_type,
    normalize_part_b_input,
)


# ---------------------------------------------------------------------------
# detect_part_b_type — классификация ожидаемого ответа по его содержимому
# ---------------------------------------------------------------------------

class TestDetectPartBType:
    def test_letters_only_is_sequence(self):
        assert detect_part_b_type("АБВ") == "sequence"

    def test_digits_only_is_multiple(self):
        assert detect_part_b_type("123") == "multiple"

    def test_mixed_letters_digits_is_correspondence(self):
        assert detect_part_b_type("А1Б2В3") == "correspondence"

    def test_two_letters_is_sequence(self):
        assert detect_part_b_type("АБ") == "sequence"

    def test_latin_letters_normalized(self):
        # Латинские буквы переводятся через translate
        assert detect_part_b_type("AB") == "sequence"

    def test_single_digit_is_multiple(self):
        assert detect_part_b_type("3") == "multiple"


# ---------------------------------------------------------------------------
# normalize_part_b_input — нормализация пользовательского ввода
# ---------------------------------------------------------------------------

class TestNormalizePartBInput:
    # correspondence
    def test_correspondence_valid(self):
        ok, result = normalize_part_b_input("А1Б2В3", "correspondence")
        assert ok is True
        assert result == "А1Б2В3"

    def test_correspondence_with_spaces(self):
        ok, result = normalize_part_b_input("А1 Б2 В3", "correspondence")
        assert ok is True
        assert "А1" in result

    def test_correspondence_missing_digits(self):
        ok, _ = normalize_part_b_input("АБВ", "correspondence")
        assert ok is False

    # sequence
    def test_sequence_letters(self):
        ok, result = normalize_part_b_input("АБВГ", "sequence")
        assert ok is True
        assert result == "АБВГ"

    def test_sequence_with_digits_rejected(self):
        ok, _ = normalize_part_b_input("А1Б2", "sequence")
        assert ok is False

    def test_sequence_latin_to_cyrillic(self):
        ok, result = normalize_part_b_input("ABВГ", "sequence")
        assert ok is True
        assert "А" in result

    # multiple
    def test_multiple_comma_separated(self):
        ok, result = normalize_part_b_input("1, 2, 4", "multiple")
        assert ok is True
        assert result == "124"

    def test_multiple_deduplicates_and_sorts(self):
        ok, result = normalize_part_b_input("3 1 3 2", "multiple")
        assert ok is True
        assert result == "123"

    def test_multiple_empty(self):
        ok, _ = normalize_part_b_input("", "multiple")
        assert ok is False

    def test_multiple_single_digit(self):
        ok, result = normalize_part_b_input("3", "multiple")
        assert ok is True
        assert result == "3"


# ---------------------------------------------------------------------------
# _detect_part_b_kind — определяет вид вопроса типа B по тексту
# ---------------------------------------------------------------------------

class TestDetectPartBKind:
    def _q(self, text: str) -> dict:
        return {"type": "B", "question_text": text, "expected": ""}

    def test_correspondence_keyword(self):
        q = self._q("Соотнесите события и даты:\nА) событие — 1) год")
        assert _detect_part_b_kind(q) == "correspondence"

    def test_sequence_keyword(self):
        q = self._q("Расставьте события в хронологическом порядке:\nА) ...\nБ) ...")
        assert _detect_part_b_kind(q) == "sequence"

    def test_multiple_via_explicit_marker(self):
        q = self._q(
            "Укажите верные утверждения. Порядок записи цифр не имеет значения.\n"
            "1) Первое\n2) Второе\n3) Третье"
        )
        assert _detect_part_b_kind(q) == "multiple"

    def test_multiple_via_vyberi(self):
        q = self._q("Выберите правильные ответы:\n1) Первое\n2) Второе\n3) Третье")
        assert _detect_part_b_kind(q) == "multiple"

    def test_open_fallback(self):
        q = self._q("Заполните пропуск: В 1919 году было создано _____________")
        assert _detect_part_b_kind(q) == "open"


# ---------------------------------------------------------------------------
# _check_partial_credit — частичный зачёт
# ---------------------------------------------------------------------------

class TestCheckPartialCreditTypeA:
    def _q_a(self, expected: str, options: dict | None = None) -> dict:
        return {
            "type": "A",
            "expected": expected,
            "options": options or {1: "один", 2: "два", 3: "три", 4: "четыре", 5: "пять"},
        }

    def test_partial_correct_subset_returns_yellow(self):
        q = self._q_a("135")
        result = _check_partial_credit(q, "13")
        assert result is not None
        assert "🟡" in result
        assert "2 из 3" in result

    def test_full_correct_returns_none(self):
        q = self._q_a("135")
        result = _check_partial_credit(q, "135")
        assert result is None  # LLM отдаёт ✅

    def test_wrong_included_returns_none(self):
        q = self._q_a("135")
        result = _check_partial_credit(q, "124")  # 2 и 4 — не верные
        assert result is None  # LLM отдаёт ❌

    def test_single_expected_no_partial(self):
        q = self._q_a("2")
        result = _check_partial_credit(q, "2")
        assert result is None

    def test_one_of_two_correct(self):
        q = self._q_a("24")
        result = _check_partial_credit(q, "2")
        assert result is not None
        assert "🟡" in result

    def test_empty_input_returns_none(self):
        q = self._q_a("135")
        result = _check_partial_credit(q, "")
        assert result is None


class TestCheckPartialCreditTypeBMultiple:
    def _q_b_multiple(self, expected: str) -> dict:
        return {
            "type": "B",
            "question_text": "Выберите правильные ответы:\n1) Первое\n2) Второе\n3) Третье",
            "expected": expected,
        }

    def test_partial_subset_returns_yellow(self):
        q = self._q_b_multiple("13")
        result = _check_partial_credit(q, "1")
        assert result is not None
        assert "🟡" in result

    def test_full_correct_returns_none(self):
        q = self._q_b_multiple("13")
        result = _check_partial_credit(q, "13")
        assert result is None

    def test_wrong_digit_returns_none(self):
        q = self._q_b_multiple("13")
        result = _check_partial_credit(q, "12")  # 2 не верный
        assert result is None

    def test_single_expected_no_partial(self):
        q = self._q_b_multiple("2")
        result = _check_partial_credit(q, "2")
        assert result is None


class TestCheckPartialCreditOtherTypes:
    def test_type_b_sequence_returns_none(self):
        q = {
            "type": "B",
            "question_text": "Расставьте в хронологическом порядке:\nА) ...\nБ) ...\nВ) ...",
            "expected": "АВБ",
        }
        result = _check_partial_credit(q, "АВ")
        assert result is None

    def test_unknown_type_returns_none(self):
        q = {"type": "C", "expected": "123", "options": {}}
        result = _check_partial_credit(q, "1")
        assert result is None
