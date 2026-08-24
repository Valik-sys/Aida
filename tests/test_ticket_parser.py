"""Тесты разбора билетов.

Основное внимание — ключу с ответами и фильтру качества: именно там
обнаружились ошибки, из-за которых терялись ответы преподавателя.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.ticket_parser import (  # noqa: E402
    ParsedQuestion,
    REASON_BAD_ANSWER,
    REASON_FEW_OPTIONS,
    REASON_NO_ANSWER,
    REASON_SHORT_TEXT,
    _normalize_expected,
    _parse_answers,
    validate,
)


class TestNormalizeExpected:
    def test_multiselect_sorted_and_deduped(self):
        # Порядок в многовыборе не важен — приводим к возрастающему
        assert _normalize_expected("531") == "135"
        assert _normalize_expected("245") == "245"

    def test_year_is_not_treated_as_multiselect(self):
        # Раньше 1795 сортировалось в 1579 — ответ становился неверным
        assert _normalize_expected("1795") == "1795"
        assert _normalize_expected("1900") == "1900"
        assert _normalize_expected("1569") == "1569"

    def test_repeated_digits_are_not_multiselect(self):
        assert _normalize_expected("1122") == "1122"

    def test_sequence_of_letters(self):
        assert _normalize_expected("БГВА") == "БГВА"
        assert _normalize_expected("вабг") == "ВАБГ"

    def test_correspondence(self):
        assert _normalize_expected("А2Б3В1Г4") == "А2Б3В1Г4"

    def test_word_answer_survives(self):
        # Раньше словесный ответ вычищался в пустую строку
        assert _normalize_expected("Метрополия") == "Метрополия"
        assert _normalize_expected("Сарматизм") == "Сарматизм"
        assert _normalize_expected("Волока;") == "Волока"

    def test_empty(self):
        assert _normalize_expected("") == ""
        assert _normalize_expected("   ") == ""


class TestParseAnswers:
    def test_semicolon_format_with_words(self):
        lines = [
            "Часть А: А1 — 4; А2 — 5; А3 — 2.",
            "Часть B: B1 — Метрополия; B2 — ВАБ; B3 — А2Б3В1Г4; B4 — 135.",
        ]
        a, b = _parse_answers(lines)
        assert a == {"А1": "4", "А2": "5", "А3": "2"}
        assert b["В1"] == "Метрополия"
        assert b["В2"] == "ВАБ"
        assert b["В3"] == "А2Б3В1Г4"
        assert b["В4"] == "135"

    def test_space_separated_format(self):
        lines = ["Часть В: В1 — А3Б2В1 В2 — БВГА В3 — 245 В4 — ВГБА"]
        _a, b = _parse_answers(lines)
        assert b == {"В1": "А3Б2В1", "В2": "БВГА", "В3": "245", "В4": "ВГБА"}

    def test_latin_part_markers(self):
        # «Часть B: B1» латиницей — раньше терялась вся часть В
        lines = ["Часть B: B1 — БГВА; B2 — 134."]
        _a, b = _parse_answers(lines)
        assert b == {"В1": "БГВА", "В2": "134"}

    def test_no_answers_block(self):
        a, b = _parse_answers(["Просто текст без ключа"])
        assert a == {} and b == {}


def _q(text="Вопрос про историю Беларуси", options=None, expected="1", part="А"):
    return ParsedQuestion(
        part=part, num="1", question_text=text,
        options=options if options is not None else ["", "", "", "", ""],
        expected=expected,
    )


class TestValidate:
    def test_good_question_with_options(self):
        assert validate(_q(options=["Первый", "Второй", "", "", ""], expected="2")) is None

    def test_good_text_answer_question(self):
        assert validate(_q(expected="БГВА")) is None

    def test_rejects_missing_answer(self):
        assert validate(_q(expected="")) == REASON_NO_ANSWER

    def test_rejects_short_text(self):
        assert validate(_q(text="Кто?")) == REASON_SHORT_TEXT

    def test_rejects_answer_pointing_at_empty_option(self):
        # Ответ «3», а третьего варианта нет — вопрос ученику показывать нельзя
        assert validate(_q(options=["Первый", "Второй", "", "", ""], expected="3")) == REASON_BAD_ANSWER

    def test_rejects_non_numeric_answer_for_options_question(self):
        assert validate(_q(options=["Первый", "Второй", "", "", ""], expected="БГВА")) == REASON_BAD_ANSWER

    def test_rejects_single_option(self):
        assert validate(_q(options=["Единственный", "", "", "", ""], expected="1")) == REASON_FEW_OPTIONS


@pytest.mark.skipif(
    not (ROOT / "data/raw/tests/Раннее Новое время.docx").exists(),
    reason="нет образцов билетов",
)
class TestOnRealFile:
    def test_full_parse_of_file_with_key(self):
        from services.ticket_parser import parse_docx

        result = parse_docx(ROOT / "data/raw/tests/Раннее Новое время.docx")
        assert result.source == "paragraphs"
        # Все 34 вопроса имеют ответ и проходят фильтр
        assert result.accepted_count == 34
        assert result.rejected_count == 0
        with_options, text_answers = result.count_by_type()
        assert with_options == 12 and text_answers == 22

    def test_file_without_key_is_fully_rejected(self):
        from services.ticket_parser import parse_docx

        result = parse_docx(ROOT / "data/raw/tests/variant_1.docx")
        assert result.total_found == 38
        assert result.accepted_count == 0
        assert result.reasons_summary() == {REASON_NO_ANSWER: 38}


@pytest.mark.skipif(
    not (ROOT / "data/raw/tests/variant_1.docx").exists(),
    reason="нет образцов билетов",
)
def test_strip_media_preserves_parsing():
    import shutil
    import tempfile

    from services.docx_tools import strip_media
    from services.ticket_parser import parse_paragraphs

    workdir = Path(tempfile.mkdtemp(prefix="aida_test_"))
    try:
        src = ROOT / "data/raw/tests/variant_1.docx"
        dst = workdir / "stripped.docx"
        before, after = strip_media(src, dst)

        assert after < before / 2, "картинки должны заметно уменьшить файл"

        original, _, _ = parse_paragraphs(src)
        stripped, _, _ = parse_paragraphs(dst)
        assert [q.question_text for q in original] == [q.question_text for q in stripped]
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
