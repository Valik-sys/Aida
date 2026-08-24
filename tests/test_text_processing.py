"""
Tests for text-processing utilities in bot/handlers/tests.py:
- _strip_answer_tail
- _normalize_latin_markers
- _normalize_expected_part_b
"""
import pytest
from bot.handlers.tests import (
    _normalize_expected_part_b,
    _normalize_latin_markers,
    _strip_answer_tail,
)


class TestStripAnswerTail:
    def test_strips_instruction_at_end_of_line(self):
        text = "Назовите причину события. Ответ запишите цифрой"
        assert _strip_answer_tail(text) == "Назовите причину события."

    def test_strips_instruction_with_trailing_dot(self):
        text = "Укажите дату. Ответ запишите словами."
        assert _strip_answer_tail(text) == "Укажите дату."

    def test_keeps_quote_after_instruction(self):
        """
        Регрессионный тест: regex не должен срезать «цитату с пропуском»,
        которая стоит ПОСЛЕ фразы «Ответ запишите словами.»
        """
        text = (
            "Заполните пропуск в предложении. Ответ запишите словами. "
            "«Временной промежуток с 1906 по 1914 г. вошел в историю "
            "как _______________ период»"
        )
        result = _strip_answer_tail(text)
        assert "период»" in result
        assert "Ответ запишите" not in result

    def test_keeps_quote_variant_spaces(self):
        text = "Вставьте пропущенное слово. Ответ запишите словами.  «Договор о ___»"
        result = _strip_answer_tail(text)
        assert "«Договор о ___»" in result

    def test_strips_trailing_otvet_colon_line(self):
        text = "Вопрос с вариантами\nА) один\nОтвет:"
        result = _strip_answer_tail(text)
        assert "Ответ" not in result

    def test_strips_inline_otvet_colon(self):
        text = "Событие произошло в Ответ:"
        result = _strip_answer_tail(text)
        assert result == "Событие произошло в"

    def test_multiline_only_strips_matching_lines(self):
        text = "Вопрос первый\nОтвет запишите цифрой\nА) один\nБ) два"
        result = _strip_answer_tail(text)
        lines = result.splitlines()
        assert "Вопрос первый" in lines[0]
        assert "А) один" in result
        assert "Ответ запишите" not in result

    def test_empty_input(self):
        assert _strip_answer_tail("") == ""

    def test_clean_text_untouched(self):
        text = "Какое событие произошло в 1918 году?\nА) Основание БССР\nБ) Другой вариант"
        assert _strip_answer_tail(text) == text

    def test_trailing_whitespace_trimmed(self):
        text = "Укажите год.   "
        assert _strip_answer_tail(text) == "Укажите год."

    def test_strips_parenthesized_answer(self):
        text = "Вопрос (Ответ: верно)"
        result = _strip_answer_tail(text)
        assert "Ответ: верно" not in result


class TestNormalizeLatinMarkers:
    def test_a_to_cyrillic(self):
        assert "А)" in _normalize_latin_markers("A) вариант")

    def test_b_to_cyrillic_v(self):
        # Латинская B → кириллическая В (не Б, визуальное сходство)
        assert "В)" in _normalize_latin_markers("B) вариант")

    def test_c_to_cyrillic(self):
        assert "С)" in _normalize_latin_markers("C) вариант")

    def test_d_to_cyrillic(self):
        assert "Д)" in _normalize_latin_markers("D) вариант")

    def test_e_to_cyrillic(self):
        assert "Е)" in _normalize_latin_markers("E) вариант")

    def test_word_with_latin_not_replaced(self):
        # "ABC" внутри слова не заменяется — lookbehind (?<![А-ЯЁа-яёA-Za-z])
        result = _normalize_latin_markers("ABCDE вариант")
        assert "ABCDE" in result

    def test_multiple_markers_in_text(self):
        text = "A) один B) два C) три"
        result = _normalize_latin_markers(text)
        assert "А)" in result
        assert "В)" in result
        assert "С)" in result


class TestNormalizeExpectedPartB:
    def test_strips_spaces_and_uppers(self):
        assert _normalize_expected_part_b("а, б, в") == "АБВ"

    def test_keeps_digits_and_cyrillic(self):
        assert _normalize_expected_part_b("А1Б2В3") == "А1Б2В3"

    def test_strips_punctuation(self):
        assert _normalize_expected_part_b("1, 2, 3") == "123"

    def test_latin_to_cyrillic(self):
        result = _normalize_expected_part_b("A1B2")
        assert result == "А1Б2"

    def test_empty(self):
        assert _normalize_expected_part_b("") == ""
