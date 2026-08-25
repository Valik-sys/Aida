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

from services import ticket_parser  # noqa: E402
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

    def test_year_made_of_small_digits_is_still_a_year(self):
        """Найдено на живом билете: «1453» превращалось в «1345».

        Цифры года 1, 4, 5, 3 выглядят ровно как выбор четырёх вариантов,
        и различить их можно только по длине и диапазону.
        """
        assert _normalize_expected("1453") == "1453"
        assert _normalize_expected("1234") == "1234"
        assert _normalize_expected("2345") == "2345"

    def test_descending_multiselect_is_still_sorted(self):
        """Проверка года не должна выключать сортировку многовыбора."""
        assert _normalize_expected("5421") == "1245"
        assert _normalize_expected("321") == "123"

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

    def test_spaces_inside_a_word_answer_are_kept(self):
        """Найдено на живом билете: «Золотая Орда» склеивалось в одно слово."""
        assert _normalize_expected("Золотая Орда") == "Золотая Орда"
        assert _normalize_expected("магдебургское  право") == "магдебургское право"

    def test_spaces_between_parts_of_one_answer_are_removed(self):
        """А в соответствии пробел — только разделитель, его убираем."""
        assert _normalize_expected("А4 Б1 В2 Г3") == "А4Б1В2Г3"
        assert _normalize_expected("Б Г А В") == "БГАВ"

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


class TestAnswersHeader:
    """Как подписан ключ в файле.

    Найдено на живом билете: заголовок «Ответы к тесту» не опознавался,
    и файл с 34 готовыми вопросами отвергался как «без ключа».
    """

    def _ticket(self, header: str):
        return [
            "Часть А",
            "А1. Кто основал город и при каком князе это произошло?",
            "1) Рогволод", "2) Всеслав", "3) Изяслав", "4) Брячислав",
            header,
            "Часть А: А1 — 2",
        ]

    def _answer_of(self, header: str):
        questions, answers_a, _ = ticket_parser.parse_lines(self._ticket(header))
        return questions, answers_a

    def test_plain_header(self):
        _q, a = self._answer_of("Ответы")
        assert a == {"А1": "2"}

    def test_header_with_a_tail(self):
        for header in ("Ответы к тесту", "Ответы на задания", "Правильные ответы"):
            _q, a = self._answer_of(header)
            assert a == {"А1": "2"}, header

    def test_key_written_on_one_line(self):
        """«Ответы: А1 — 2» одной строкой: раньше ключ терялся целиком."""
        lines = self._ticket("Ответы: А1 — 2")[:-1]
        _questions, answers_a, _ = ticket_parser.parse_lines(lines)
        assert answers_a == {"А1": "2"}

    def test_questions_stay_above_the_key(self):
        """Заголовок отрезает вопросы от ключа, а не съедает их."""
        questions, _a = self._answer_of("Ответы к тесту")
        assert len(questions) == 1

    def test_unusual_header_still_finds_the_key(self):
        """Запасной путь: ключ ищется по форме строки, а не по подписи.

        Перечислять все написания заголовка бесполезно — преподаватель
        напишет «Эталоны» или «Ключ к варианту 3». Сама строка ключа при
        этом выглядит одинаково всегда.
        """
        for header in ("Эталоны", "Верные варианты", "Ключ к варианту 3"):
            lines = [
                "Часть А",
                "А1. Кто основал город и при каком князе это произошло?",
                "1) Рогволод", "2) Всеслав", "3) Изяслав", "4) Брячислав",
                header,
                "Часть А: А1 — 2; А2 — 3",
            ]
            _questions, answers_a, _ = ticket_parser.parse_lines(lines)
            assert answers_a == {"А1": "2", "А2": "3"}, header

    def test_key_without_any_header_is_found(self):
        lines = [
            "Часть А",
            "А1. Кто основал город и при каком князе это произошло?",
            "1) Рогволод", "2) Всеслав", "3) Изяслав", "4) Брячислав",
            "Часть А: А1 — 2; А2 — 3",
        ]
        questions, answers_a, _ = ticket_parser.parse_lines(lines)

        assert answers_a == {"А1": "2", "А2": "3"}
        # Строка ключа не должна съесть вопрос, стоящий выше
        assert len(questions) == 1

    def test_question_text_is_not_mistaken_for_a_key(self):
        """Одна пара «А1 —» встречается и в тексте: «задание А1 — простое».

        Отсюда цена страховки: ключ без заголовка находится начиная с двух
        ответов. Билет с одним-единственным вопросом придётся подписать —
        но таких билетов не бывает, а ложный ключ обрубил бы настоящий.
        """
        assert not ticket_parser.looks_like_answer_key(
            "Задание А1 — самое простое в этой части"
        )
        assert not ticket_parser.looks_like_answer_key("1) Рогволод 2) Всеслав")
        assert ticket_parser.looks_like_answer_key("Часть А: А1 — 2; А2 — 3")
        assert ticket_parser.looks_like_answer_key("В1 — 1453; В2 — шляхта")

    def test_long_sentence_is_not_a_header(self):
        """Иначе «Ответы записывайте в бланк…» обрубало бы весь билет."""
        long_line = (
            "Ответы записывайте в бланк ответов печатными буквами, "
            "начиная с первой клетки, не выходя за её границы, и следите "
            "за тем, чтобы номер задания в бланке совпадал с номером "
            "задания в тексте работы, иначе ответ не будет засчитан вовсе"
        )
        lines = ["Часть А", "А1. Вопрос про историю?", "1) раз", "2) два", long_line]
        _questions, answers_a, answers_b = ticket_parser.parse_lines(lines)
        assert answers_a == {} and answers_b == {}


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
