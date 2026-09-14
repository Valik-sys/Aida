"""Совпадение ответа с эталоном — без обращения к модели.

Появилось после первого живого теста 10.09.2026: преподаватель выбрала
«1 2 4», получила «❌ ты ошибся», и в том же сообщении бот перечислил
правильными 1, 2 и 4. Проверка целиком висела на `gpt-4.1-nano`, и она
ошиблась на точном совпадении строк.

Главное здесь — **несовпадение никогда не становится совпадением**:
функция умеет только досрочно сказать «верно». Всё, в чём она не уверена,
уходит модели, то есть работает как раньше.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.llm import answers_match  # noqa: E402


class TestTheReportedCase:
    def test_exact_multiple_choice(self):
        # Ровно тот ответ, на котором бот ошибся на созвоне
        assert answers_match("124", "124") is True

    def test_order_inside_multiple_choice_does_not_matter(self):
        assert answers_match("142", "124") is True
        assert answers_match("421", "124") is True

    @pytest.mark.parametrize("typed", ["1 2 4", "1,2,4", "1, 2, 4", "1;2;4", "1.2.4"])
    def test_separators_the_student_might_use(self, typed):
        assert answers_match(typed, "124") is True


class TestWrongStaysWrong:
    """Функция умеет только зачесть верный ответ. Ошибиться в другую
    сторону — засчитать неверный — она не должна."""

    def test_different_set(self):
        assert answers_match("124", "125") is False

    def test_incomplete_answer(self):
        assert answers_match("12", "124") is False

    def test_extra_option(self):
        assert answers_match("1245", "124") is False

    def test_empty_answer(self):
        assert answers_match("", "124") is False
        assert answers_match("   ", "124") is False

    def test_no_reference_answer(self):
        # Эталона нет — сравнивать не с чем, пусть разбирается модель
        assert answers_match("124", "") is False


class TestYears:
    """У года и многовыбора одни и те же цифры, но 1453 и 1345 — разные годы."""

    def test_same_digits_different_year(self):
        assert answers_match("1453", "1345") is False

    def test_exact_year(self):
        assert answers_match("1453", "1453") is True

    def test_year_with_spaces(self):
        assert answers_match(" 1795 ", "1795") is True


class TestWordAnswers:
    def test_case_does_not_matter(self):
        assert answers_match("Метрополия", "метрополия ") is True

    def test_two_words(self):
        assert answers_match("Золотая Орда", "золотая орда") is True

    def test_different_words_are_not_equal(self):
        assert answers_match("Метрополия", "Колония") is False


class TestLettersAndPairs:
    def test_latin_letter_is_read_as_cyrillic(self):
        # «A2Б3В1» с латинской A — та же строка, что и кириллическая
        assert answers_match("А2Б3В1", "A2Б3В1") is True

    def test_sequence_order_matters(self):
        assert answers_match("БГВА", "БГВА") is True
        # Порядок в последовательности — часть ответа, сверять множеством
        # нельзя. Такой случай уходит модели, а не зачитывается молча
        assert answers_match("БГВА", "ВГБА") is False
