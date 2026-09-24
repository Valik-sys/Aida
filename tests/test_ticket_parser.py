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


class TestPartHeadersAndKeyEnd:
    """Две поломки, найденные 11.09.2026 при сборке шаблона для преподавателя.

    Обе тихие: файл разбирается, ошибок не видно, а у ученика на экране
    оказывается мусор — лишний вариант ответа или чужой правильный ответ.
    """

    def test_part_header_with_colon_is_not_an_option(self):
        """«Часть В:» прилипала пятым вариантом к последнему вопросу части А:
        граница частей узнавалась только без двоеточия, а пишут его почти всегда."""
        from services.ticket_parser import parse_lines

        questions, _a, _b = parse_lines([
            "Часть А:", "",
            "А1. Кто основал Полоцкое княжество?",
            "1) Рогволод", "2) Всеслав", "3) Изяслав", "4) Брячислав", "",
            "Часть В:", "",
            "В1. Укажите год первого упоминания Полоцка.", "",
            "Ответы:", "А1 — 1; В1 — 862",
        ])

        by_marker = {f"{q.part}{q.num}": q for q in questions}
        options = [o for o in by_marker["А1"].options if o.strip()]
        assert options == ["Рогволод", "Всеслав", "Изяслав", "Брячислав"]
        assert all("Часть" not in o for o in options)
        assert by_marker["В1"].expected == "862"

    def test_text_under_the_key_does_not_overwrite_answers(self):
        """Подпись под ключом ломала ключ: в строке «А2Б3В1 — соответствие»
        разбор видел пару «В1 — соответствие» и переписывал ей настоящий ответ."""
        from services.ticket_parser import parse_lines

        questions, _a, _b = parse_lines([
            "В1. Определите три правильных утверждения о периодизации:", "",
            "Ответы:", "В1 — 135",
            "",
            "Пояснение к ключу:",
            "2 — номер варианта",
            "А2Б3В1 — соответствие",
            "БГВА — последовательность",
        ])

        assert questions[0].expected == "135"

    def test_caption_above_the_key_still_works(self):
        """Над ключом подпись бывает законно — она обрывать разбор не должна."""
        from services.ticket_parser import parse_lines

        questions, _a, _b = parse_lines([
            "А1. Кто основал Полоцкое княжество?",
            "1) Рогволод", "2) Всеслав", "",
            "Ответы:",
            "Правильные ответы к варианту 1:",
            "А1 — 1",
        ])

        assert questions[0].expected == "1"

    def test_key_split_by_parts_survives(self):
        """Ключ, разложенный по частям с пустыми строками, — обычное дело."""
        from services.ticket_parser import parse_lines

        questions, _a, _b = parse_lines([
            "А1. Вопрос с вариантами ответа, достаточно длинный.",
            "1) первый", "2) второй", "",
            "В1. Открытый вопрос, тоже достаточно длинный.", "",
            "Ответы:",
            "Часть А:", "А1 — 2",
            "",
            "Часть В:", "В1 — Метрополия",
        ])

        by_marker = {f"{q.part}{q.num}": q for q in questions}
        assert by_marker["А1"].expected == "2"
        assert by_marker["В1"].expected == "Метрополия"


class TestHandTypedOptionNumbers:
    """Варианты с номером, набранным руками без скобки: «1 Хозяйство…».

    Бот нумерует варианты сам, и номер из файла выходил дважды:
    «1️⃣ 1 Хозяйство…» (клиент, 17.09.2026).
    """

    def _options(self, lines):
        from services.ticket_parser import parse_lines

        questions, _a, _b = parse_lines(
            ["А1. Что такое натуральное хозяйство?"] + lines + ["Ответы:", "А1 — 2; А2 — 1"]
        )
        return [o for o in questions[0].options if o]

    def test_space_after_number(self):
        assert self._options([
            "1 Хозяйство, ориентированное на продажу",
            "2 Хозяйство для собственных нужд",
            "3 Хозяйство с наёмным трудом",
        ]) == [
            "Хозяйство, ориентированное на продажу",
            "Хозяйство для собственных нужд",
            "Хозяйство с наёмным трудом",
        ]

    def test_tab_and_dot_after_number(self):
        assert self._options(["1\tРим", "2\tАфины"]) == ["Рим", "Афины"]
        assert self._options(["1. Рим", "2. Афины"]) == ["Рим", "Афины"]

    def test_option_starting_with_a_number_is_kept(self):
        """Дата в варианте — не номер: лесенки 1, 2, 3 у соседей нет."""
        # Точку в конце варианта парсер срезает всегда — это не про номер
        assert self._options([
            "1 сентября 1939 г.",
            "22 июня 1941 г.",
        ]) == ["1 сентября 1939 г", "22 июня 1941 г"]

    def test_decimal_is_not_a_number_marker(self):
        assert self._options(["1.5 млн человек", "2.5 млн человек"]) == [
            "1.5 млн человек", "2.5 млн человек",
        ]

    def test_plain_options_untouched(self):
        assert self._options(["Рим", "Афины"]) == ["Рим", "Афины"]

    def test_answer_still_points_to_the_right_option(self):
        from services.ticket_parser import parse_lines, validate

        questions, _a, _b = parse_lines([
            "А1. Столицей Византийской империи был город:",
            "1 Рим", "2 Афины", "3 Константинополь", "4 Никея",
            "Ответы:", "А1 — 3",
        ])
        q = questions[0]
        assert q.options[q_index(q.expected)] == "Константинополь"
        assert validate(q) is None


def q_index(expected: str) -> int:
    return int(expected) - 1


class TestAnswerUnderQuestion:
    """Ответ прямо под вопросом, без ключа в конце.

    Клиент, 24.09.2026: «бот не принимает файл, если стоят ответы после
    каждого вопроса сразу». Строка «Ответ 5» уходила пятым вариантом,
    у вопроса не было ответа, и фильтр отбрасывал весь файл.
    """

    def _parse(self, lines):
        from services.ticket_parser import parse_lines

        questions, _a, _b = parse_lines(lines)
        return {f"{q.part}{q.num}": q for q in questions}

    def test_part_a_and_b(self):
        qs = self._parse([
            "Часть А",
            "А1. Возможная прародина человека:",
            "1) Северная Америка    2) Южная Америка    3) Европа   4) Австралия    5) Африка",
            "Ответ 5",
            "А2. Кто основал Полоцкое княжество?",
            "1) Рогволод", "2) Всеслав", "3) Изяслав",
            "Ответ: 1",
            "Часть В",
            "В1. Расставьте периоды истории в хронологической последовательности.",
            "А) Новейшее время", "Б) Средние века", "В) Новое время", "Г) Древний мир",
            "Ответ  ГБВА",
            "В2. Глава рода, наиболее опытный человек - ______________",
            "Ответ СТАРЕЙШИНА.",
            "В3. Выберите правильные утверждения о соседской общине:",
            "1) первое", "2) второе", "3) третье", "4) четвёртое", "5) пятое",
            "Отв. — 5, 3, 1",
        ])
        assert qs["А1"].expected == "5"
        assert qs["А1"].options == ["Северная Америка", "Южная Америка", "Европа", "Австралия", "Африка"]
        assert qs["А2"].expected == "1"
        assert all("Ответ" not in o for q in qs.values() for o in q.options)
        assert qs["В1"].expected == "ГБВА"
        assert "Ответ" not in qs["В1"].question_text
        assert qs["В2"].expected == "СТАРЕЙШИНА"
        assert qs["В3"].expected == "135"
        assert all(validate(q) is None for q in qs.values())

    def test_key_at_the_end_wins(self):
        """Решено 24.09.2026: если есть и то и другое, прав ключ — как раньше."""
        qs = self._parse([
            "А1. Кто основал Полоцкое княжество?",
            "1) Рогволод", "2) Всеслав", "3) Изяслав",
            "Ответ 2",
            "Ответы:", "А1 — 1",
        ])
        assert qs["А1"].expected == "1"

    def test_instruction_is_not_an_answer(self):
        """«Ответ запишите цифрами…» — часть задания, а не ответ."""
        qs = self._parse([
            "В1. Определите группы индоевропейских народов Европы.",
            "Ответ запишите цифрами в порядке возрастания. Например: 123.",
            "1) италики;   2) армяне;    3) греки;     4) кельты;     5) балты.",
            "Ответ 145",
        ])
        assert qs["В1"].expected == "145"
        assert "Ответ запишите цифрами" in qs["В1"].question_text

    def test_answer_at_the_end_of_the_line(self):
        qs = self._parse([
            "В1. Община, в которой родство велось по женской линии, - это ___ род. Ответ  МАТЕРИНСКИЙ",
        ])
        assert qs["В1"].expected == "МАТЕРИНСКИЙ"
        assert qs["В1"].question_text.endswith("род.")

    def test_lowercase_answer_mid_sentence_is_kept_in_text(self):
        """В середине задания «ответ» со строчной буквы — не ответ."""
        qs = self._parse([
            "В1. Укажите год, ответ дайте числом: Люблинская уния была заключена в",
            "Ответ 1569",
        ])
        assert qs["В1"].expected == "1569"
        assert "ответ дайте числом" in qs["В1"].question_text

    def test_empty_answer_blank_is_dropped(self):
        """Бланк «Ответ:» под заданием — не ответ и не текст вопроса."""
        qs = self._parse([
            "В1. Установите последовательность: А) Крево Б) Люблин В) Грюнвальд",
            "Ответ:",
            "Ответы:", "В1 — АВБ",
        ])
        assert qs["В1"].expected == "АВБ"
        assert "Ответ" not in qs["В1"].question_text

    def test_preposition_before_dates_is_not_a_key(self):
        """«в 24-23 тыс. … в 22-21-м тыс.» принималось за строку ключа
        «В24 — …», и всё ниже неё выпадало: из 80 вопросов нашлось 25."""
        qs = self._parse([
            "А1. Стоянка возле деревни Бердыж появилась:",
            "1)  40‒35 тыс. лет назад;      2)  в 24-23 тыс. до н. э.;     3)  в 22-21-м тыс. до н. э.;",
            "4)  в начале 2-го тыс. до н. э.;     5)  в VII в. до н. э.",
            "Ответ 3",
            "А2. На территории Беларуси в шахтах добывали:",
            "1) серебро;   2) кремень;     3) соль;   4) торф;        5) олово.",
            "Ответ 2",
        ])
        assert set(qs) == {"А1", "А2"}
        assert qs["А1"].options[4] == "в VII в. до н. э"
        assert qs["А2"].expected == "2"


class TestOptionsLayout:
    def _parse(self, lines):
        from services.ticket_parser import parse_lines

        questions, _a, _b = parse_lines(lines + ["Ответы:", "А1 — 4"])
        return questions[0]

    def test_list_above_options_stays_in_question(self):
        """Перечень событий над вариантами-кодами раньше затирался вариантами."""
        q = self._parse([
            "А1.  Расставьте в правильной последовательности:",
            "1) заселение Европы индоевропейцами",
            "2) появление кроманьонцев",
            "3) неолитическая революция на Ближнем Востоке",
            "4) появление соседской общины",
            "1) 2431            2) 3214          3) 2134           4) 2314",
        ])
        assert q.options[:4] == ["2431", "3214", "2134", "2314"]
        assert "1) заселение Европы индоевропейцами" in q.question_text
        assert "4) появление соседской общины" in q.question_text

    def test_wrapped_question_tail_is_not_lost(self):
        q = self._parse([
            "А1. Одним из первых занятий древних людей на территории Беларуси",
            "являлось(-ась):",
            "1) земледелие;    2) торговля;    3) охота;    4) ремесло;    5) животноводство.",
        ])
        assert q.question_text.endswith("являлось(-ась):")
        assert q.options == ["земледелие", "торговля", "охота", "ремесло", "животноводство"]


def _tmp_dir() -> Path:
    import tempfile

    return Path(tempfile.mkdtemp(prefix="aida_parser_"))


class TestWordLayout:
    """Автонумерация Word, перенос строки в абзаце и таблица внутри задания."""

    @staticmethod
    def _el(tag, **attrs):
        from docx.oxml import OxmlElement
        from docx.oxml.ns import qn

        e = OxmlElement(tag)
        for k, v in attrs.items():
            e.set(qn(f"w:{k}"), v)
        return e

    def _add_list(self, doc, num_id, start=1):
        """Список «%1)» с началом start — как Word заводит его под вопросом."""
        numbering = doc.part.numbering_part.element
        abstract = self._el("w:abstractNum", abstractNumId=str(100 + num_id))
        lvl = self._el("w:lvl", ilvl="0")
        lvl.append(self._el("w:start", val=str(start)))
        lvl.append(self._el("w:numFmt", val="decimal"))
        lvl.append(self._el("w:lvlText", val="%1)"))
        abstract.append(lvl)
        numbering.insert(0, abstract)
        num = self._el("w:num", numId=str(num_id))
        num.append(self._el("w:abstractNumId", val=str(100 + num_id)))
        numbering.append(num)

    def _numbered(self, doc, text, num_id):
        p = doc.add_paragraph(text)
        num_pr = self._el("w:numPr")
        num_pr.append(self._el("w:ilvl", val="0"))
        num_pr.append(self._el("w:numId", val=str(num_id)))
        p._p.get_or_add_pPr().append(num_pr)
        return p

    def test_client_layout(self):
        import docx
        from services.ticket_parser import parse_docx

        doc = docx.Document()
        self._add_list(doc, 91)
        self._add_list(doc, 92)
        self._add_list(doc, 93, start=4)
        self._add_list(doc, 94)

        doc.add_paragraph("Часть А")
        doc.add_paragraph("А1. Необходимость объединения людей в родовые общины объясняется:")
        for text in ["созданием первых государств;", "сложными условиями жизни;",
                     "ведением производящего хозяйства;", "появлением религиозных верований;"]:
            self._numbered(doc, text, 91)
        # Ответ после переноса строки (Shift+Enter) в последнем варианте
        self._numbered(doc, "увеличением численности населения.\n Ответ 2", 91)

        doc.add_paragraph("А2. Примерно в III тыс. до н.э. люди на территории Беларуси начали:")
        self._numbered(doc, "строить города;    2) выплавлять железо;    3) создавать государства;", 92)
        self._numbered(doc, "заниматься животноводством;     5) объединяться в племена.", 93)
        doc.add_paragraph("Ответ 4")

        doc.add_paragraph("Часть В")
        doc.add_paragraph("В1. Выберите правильные утверждения. Ответ запишите цифрами.")
        for text in ["первое утверждение;", "второе утверждение;", "третье утверждение."]:
            self._numbered(doc, text, 94)
        doc.add_paragraph("Ответ 13")

        doc.add_paragraph("В2. Установите соответствие.")
        table = doc.add_table(rows=2, cols=2)
        table.cell(0, 0).text = "Событие"
        table.cell(0, 1).text = "Период"
        table.cell(1, 0).text = "А) переход к оседлости\nБ) возникновение металлургии"
        table.cell(1, 1).text = "1) палеолит\n2) бронзовый век\n3) мезолит"
        doc.add_paragraph("Ответ  А3Б2")

        path = _tmp_dir() / "client.docx"
        doc.save(path)

        result = parse_docx(path)
        assert result.rejected == []
        rows = {f"{r['Часть']}{r['№']}": r for r in result.rows}

        a1 = rows["А1"]
        assert a1["Ответ"] == "2"
        assert a1["Вар.1"] == "созданием первых государств"
        assert a1["Вар.5"] == "увеличением численности населения"

        a2 = rows["А2"]
        assert a2["Ответ"] == "4"
        assert [a2[f"Вар.{i}"] for i in range(1, 6)] == [
            "строить города", "выплавлять железо", "создавать государства",
            "заниматься животноводством", "объединяться в племена",
        ]

        # Номера автосписка видны ученику: без них «13» не к чему отнести
        b1 = rows["В1"]["Вопрос"]
        assert "1) первое утверждение;" in b1
        assert "3) третье утверждение." in b1

        # Таблица читается внутри задания, по столбцам
        assert rows["В2"]["Вопрос"].split("\n") == [
            "Установите соответствие.",
            "Событие", "А) переход к оседлости", "Б) возникновение металлургии",
            "Период", "1) палеолит", "2) бронзовый век", "3) мезолит",
        ]
        assert rows["В2"]["Ответ"] == "А3Б2"

    def test_style_numbering_restarts_per_question(self):
        """«Нумерованный список» стилем — один список на весь документ.
        Второй вопрос не должен получить варианты 4)–6)."""
        import docx
        from services.ticket_parser import parse_docx

        doc = docx.Document()
        doc.add_paragraph("А1. Кто основал Полоцкое княжество?")
        for text in ["Рогволод", "Всеслав", "Изяслав"]:
            doc.add_paragraph(text, style="List Number")
        doc.add_paragraph("Ответ 1")
        doc.add_paragraph("А2. Столица Великого княжества Литовского:")
        for text in ["Вильно", "Полоцк", "Новогрудок"]:
            doc.add_paragraph(text, style="List Number")
        doc.add_paragraph("Ответ 1")
        path = _tmp_dir() / "styled.docx"
        doc.save(path)

        rows = {r["№"]: r for r in parse_docx(path).rows}
        assert [rows["2"][f"Вар.{i}"] for i in range(1, 4)] == ["Вильно", "Полоцк", "Новогрудок"]
