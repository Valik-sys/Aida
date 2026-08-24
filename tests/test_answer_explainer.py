"""Тесты защиты от приписанных фактов в объяснении ответа.

Смысл проверки: модель может переформулировать и связать то, что уже есть
в вопросе и ответе преподавателя, но не может притащить факт со стороны.
Здесь проверяется и то, что подлог ловится, и то, что честное объяснение
не отбрасывается зря из-за склонений.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.answer_explainer import (  # noqa: E402
    build_source_text,
    find_unsupported_facts,
)


QUESTION = "Какое событие произошло в 1569 г. и объединило ВКЛ и Польшу?"
OPTIONS = {1: "Кревская уния", 2: "Городельская уния", 3: "Люблинская уния"}
ANSWER = "3"

SOURCE = build_source_text(QUESTION, OPTIONS, ANSWER)


class TestCatchesInventedFacts:
    def test_catches_year_not_in_question(self):
        text = "Верный ответ — Люблинская уния, подписанная в 1385 году."
        assert "1385" in find_unsupported_facts(text, SOURCE)

    def test_catches_invented_name(self):
        text = "Ты ошибся. Унию подписал Сигизмунд, объединив государства."
        assert "Сигизмунд" in find_unsupported_facts(text, SOURCE)

    def test_catches_invented_century(self):
        text = "Это событие относится к XVIII веку по общей хронологии."
        assert "XVIII" in find_unsupported_facts(text, SOURCE)

    def test_catches_several_at_once(self):
        text = "Речь Посполитая возникла в 1772 году при короле Стефане."
        found = find_unsupported_facts(text, SOURCE)
        assert "1772" in found
        assert "Стефане" in found

    def test_catches_invented_acronym(self):
        """Аббревиатуры пишутся целиком заглавными и раньше не проверялись."""
        text = "Ответ третий, позже эти земли вошли в СССР."
        assert "СССР" in find_unsupported_facts(text, SOURCE)

    def test_catches_acronym_at_sentence_start(self):
        """Аббревиатура однозначна, её проверяем и в начале предложения."""
        text = "Верный ответ третий. БССР появилась значительно позже."
        assert "БССР" in find_unsupported_facts(text, SOURCE)

    def test_catches_year_at_sentence_start(self):
        text = "Верный ответ третий. 1772 год стал началом разделов."
        assert "1772" in find_unsupported_facts(text, SOURCE)


class TestDoesNotFlagHonestExplanations:
    def test_plain_restatement_passes(self):
        text = "Верный вариант третий. Именно он говорит об объединении двух государств."
        assert find_unsupported_facts(text, SOURCE) == []

    def test_year_from_question_is_allowed(self):
        text = "В 1569 году произошло объединение, поэтому верен третий вариант."
        assert find_unsupported_facts(text, SOURCE) == []

    def test_declined_forms_are_allowed(self):
        """Русский склоняет: «Люблинской» против «Люблинская» — это одно слово."""
        text = "Речь идёт о Люблинской унии, которая и объединила эти государства."
        assert find_unsupported_facts(text, SOURCE) == []

    def test_short_declined_words_are_allowed(self):
        """Короткие слова тоже склоняются: «Речью» против «Речи».

        Сравнение по обрезке начала слова здесь ошибалось и выбрасывало
        нормальное объяснение.
        """
        source = build_source_text(
            "Что объединило ВКЛ и Речь Посполитую?",
            {1: "Кревская уния", 2: "Люблинская уния"},
            "2",
        )
        text = "Ответ второй, речь о Речью Посполитой и её образовании."
        assert find_unsupported_facts(text, source) == []

    def test_acronym_from_question_is_allowed(self):
        source = build_source_text("Когда образовалась БССР?", None, "1919")
        text = "Верный ответ — 1919 год, именно тогда БССР и появилась."
        assert find_unsupported_facts(text, source) == []

    def test_common_word_at_sentence_start_is_not_flagged(self):
        """Любое обычное слово с заглавной в начале — не имя собственное."""
        text = "Ответ третий. Объединение двух государств произошло именно так."
        assert find_unsupported_facts(text, SOURCE) == []

    def test_capitalized_sentence_start_is_not_a_name(self):
        text = "Правильный ответ третий. Объединение произошло именно тогда."
        assert find_unsupported_facts(text, SOURCE) == []

    def test_abbreviation_from_question_allowed(self):
        text = "Ответ третий, потому что речь про ВКЛ и Польшу."
        assert find_unsupported_facts(text, SOURCE) == []

    def test_empty_explanation(self):
        assert find_unsupported_facts("", SOURCE) == []


class TestSourceText:
    def test_includes_question_options_and_answer(self):
        source = build_source_text("Вопрос", {1: "Первый", 2: "Второй"}, "БГВА")
        assert "Вопрос" in source
        assert "Первый" in source and "Второй" in source
        assert "БГВА" in source

    def test_works_without_options(self):
        source = build_source_text("Вопрос", None, "1795")
        assert "Вопрос" in source and "1795" in source

    def test_answer_year_is_allowed_in_explanation(self):
        """Ответ-год преподавателя не должен считаться приписанным фактом."""
        source = build_source_text("Укажи год события", None, "1795")
        text = "Верный ответ — 1795 год, именно тогда это и случилось."
        assert find_unsupported_facts(text, source) == []
