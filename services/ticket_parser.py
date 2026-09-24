"""Разбор билетов из .docx — ядро, вынесенное из scripts/parse_tests.py.

Чистая функция «файл → строки в схеме таблицы тестов», без Google Sheets и CLI.
Скрипт scripts/parse_tests.py остаётся тонкой обёрткой поверх этого модуля.

Вопросы НЕ генерируются — только структурируется то, что написал преподаватель.

Поверх разбора работает фильтр качества: в тренажёр попадает только целый
вопрос. Всё сомнительное отсекается и складывается в rejected с причиной —
лучше меньше вопросов, чем один кривой у ученика.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from docx import Document

from services import pdf_tools


logger = logging.getLogger(__name__)

# Версия парсера пишется в манифест. При изменении логики разбора поднять —
# тогда видно, какие файлы разобраны устаревшей версией и требуют перепрогона.
# 2 — чтение PDF с текстовым слоем.
# 3 — заголовок ключа не обязан быть словом «Ответы» в одиночку, год больше
#     не сортируется как многовыбор, пробелы в словесном ответе сохраняются.
#     Накопленное стоит перегнать через /reparse: разбор изменился.
# 4 — «Часть В:» с двоеточием снова граница частей, а не вариант ответа;
#     ключ кончается там, где начинается обычный текст под ним.
# 5 — ручная нумерация вариантов без скобки («1 Хозяйство…») срезается,
#     иначе номер выходил дважды. Загруженное надо перегнать через /reparse.
# 6 — ответ прямо под вопросом («Ответ 5»), автонумерация Word у вариантов,
#     перенос строки внутри абзаца. Загруженное надо перегнать через /reparse.
PARSER_VERSION = 6

# Схема строки — та же, что в таблице тестов проекта. Менять нельзя:
# на неё завязаны хендлеры тестов.
COLS = [
    "Вариант", "Часть", "№", "Вопрос",
    "Вар.1", "Вар.2", "Вар.3", "Вар.4", "Вар.5",
    "Ответ", "Раздел",
]

# А/В — кириллица, A/B — латиница (бывает в docx)
_question_marker_re = re.compile(r"^\s*([АВABаваb])\s*(\d+)\s*[.\)]\s*(.*)$")
# Заголовок ключа. Пишут его по-разному: «Ответы», «Ответы к тесту»,
# «Правильные ответы», «Ключ», иногда сразу с ответами в той же строке
# («Ответы: А1 — 2; А2 — 3»). Хвост забирается отдельно, иначе ключ,
# записанный одной строкой, терялся бы целиком.
#
# Длина хвоста ограничена: без ограничения заголовком становилась бы любая
# фраза вроде «Ответы записывайте в бланк ответов печатными буквами», и
# всё, что ниже, парсер счёл бы ключом.
_ANSWERS_TAIL_LIMIT = 200

_answers_header_re = re.compile(
    r"^\s*(?:правильные\s+|верные\s+)?(?:ответы|ключ(?:\s+ответов)?)"
    r"(?:\s+(?:к|на|для)\s+\S+)?"
    r"\s*[:\-–—]?\s*(?P<tail>.*)$",
    re.IGNORECASE,
)
# Колонтитулы страниц: "Вариант 1  1"
_page_header_re = re.compile(r"^Вариант\s+\d+\s+\d+$", re.IGNORECASE)
# Граница частей в теле файла: «Часть А», «Часть В:», «Часть B —».
# Двоеточие после буквы обязательным не является, но пишут его почти всегда,
# и без него в шаблоне такая строка прилипала пятым вариантом к предыдущему
# вопросу — ученик видел вариант ответа «Часть В:».
_part_boundary_re = re.compile(
    r"^\s*Часть\s*[АБВAB]\s*[:.\-–—]?\s*$", re.IGNORECASE
)

# Ответ прямо под вопросом: «Ответ 5», «Ответ: 124», «Отв. — ГБВА»,
# «Правильный ответ: Вильно». Так пишут те, кто печатает билеты для себя:
# ответ рядом с вопросом, ключа в конце нет (клиент, 24.09.2026 — «бот
# не принимает файл, если ответы стоят после каждого вопроса»).
#
# Слово «Ответ» встречается и в самом задании: «Ответ запишите цифрами
# в порядке возрастания». Такая строка ответом не считается — её выдаёт
# глагол-инструкция сразу после слова и длина.
_INLINE_ANSWER_MAX = 60
_inline_answer_re = re.compile(
    r"^(?:правильный\s+|верный\s+)?(?:ответ(?![а-яё])|отв\.)\s*[:.\-–—]?\s*(?P<val>\S.*?)\s*$",
    re.IGNORECASE,
)
_answer_instruction_re = re.compile(
    r"^(?:запиш|впиш|пиш|дай|укаж|введ|выбер|отмет|обвед|округл|обоснуй|поясн"
    r"|долж|нуж|надо|в\s+вид|следует|необходимо)",
    re.IGNORECASE,
)
# Ответ в конце строки с текстом: «…это ___ род. Ответ  МАТЕРИНСКИЙ».
# Здесь условие строже, чем для отдельной строки: слово «Ответ» посреди
# задания встречается часто, поэтому ответом считается только то, что
# ответом выглядит — номера, буквы с цифрами, слово заглавными.
_answer_tail_re = re.compile(
    r"^(?P<body>.*\S)\s+(?:Ответ|ОТВЕТ)\s*[:\-–—]?\s*"
    r"(?P<val>\d{1,6}|[АБВГДЕ]{2,6}|(?:[АБВГДЕ]\d){2,}|[А-ЯЁ]{2,}(?:[\s-][А-ЯЁ]{2,})*)\.?\s*$"
)


def inline_answer(line: str) -> Optional[str]:
    """Ответ, если строка — это «Ответ …» под вопросом, иначе None.

    Пустая заготовка «Ответ:» (бланк, куда ученик впишет ответ) даёт "":
    строка не ответ, но и в текст вопроса ей идти незачем.
    """
    text = (line or "").strip()
    if re.fullmatch(r"(?:ответ|отв\.)\s*[:.\-–—]*", text, flags=re.IGNORECASE):
        return ""
    m = _inline_answer_re.match(text)
    if not m:
        return None
    val = m.group("val").strip()
    if len(val) > _INLINE_ANSWER_MAX or _answer_instruction_re.match(val):
        return None
    return val


def _split_answer_tail(line: str) -> Tuple[str, Optional[str]]:
    """Отрезает ответ, дописанный в конец строки задания."""
    m = _answer_tail_re.match(line or "")
    if not m:
        return line, None
    return m.group("body"), m.group("val")


# Задания, которые НЕ разбиваются на варианты
_no_split_re = re.compile(
    r"(определите\s+(верно\s+)?последовательность"
    r"|верно\s+соотнесите"
    r"|установите\s+соответствие"
    r"|соотнесите\s+элементы)",
    re.IGNORECASE,
)

_latin_to_cyr = str.maketrans({"A": "А", "B": "Б", "C": "В", "D": "Г"})

_LETTER_TO_NUM: Dict[str, int] = {
    "А": 1, "Б": 2, "В": 3, "Г": 4, "Д": 5,
    "а": 1, "б": 2, "в": 3, "г": 4, "д": 5,
    "A": 1, "B": 2, "C": 3, "D": 4, "E": 5,
    "a": 1, "b": 2, "c": 3, "d": 4, "e": 5,
}

_option_marker_re = re.compile(
    r"(?:^|(?<=\s)|(?<=;)|(?<=\.)|(?<=\t))"
    r"([1-5АБВГДабвгдABCDEabcde])\s*\)\s*"
)


# ---------- Чтение документа ----------

# Буквы для автонумерации Word. Русский алфавит в списках Word идёт без
# «ё», «й», «ъ», «ы», «ь» — так же, как пишут варианты руками.
_RU_LIST_LETTERS = "абвгдежзиклмнопрстуфхцчшщэюя"
_LATIN_LIST_LETTERS = "abcdefghijklmnopqrstuvwxyz"


def _list_label(fmt: str, n: int) -> Optional[str]:
    if fmt == "decimal":
        return str(n)
    letters = {
        "russianLower": _RU_LIST_LETTERS,
        "russianUpper": _RU_LIST_LETTERS.upper(),
        "lowerLetter": _LATIN_LIST_LETTERS,
        "upperLetter": _LATIN_LIST_LETTERS.upper(),
    }.get(fmt)
    if letters and 1 <= n <= len(letters):
        return letters[n - 1]
    return None


class _ListNumbering:
    """Номера автоматических списков Word.

    Word не хранит номер в тексте абзаца: «1)» рисуется при показе. Без этого
    варианты, набранные автосписком, приходили голым текстом — и в части В,
    где варианты стоят в тексте вопроса, ученик видел перечень без номеров
    при ответе «134» (клиент, 24.09.2026).

    Счёт ведётся по каждому списку отдельно, с его начального номера:
    у каждого вопроса свой список, а продолжение («4) … 5) …» на второй
    строке) заводится как список с началом 4. Абзацы надо подавать в порядке
    документа — иначе счёт собьётся.
    """

    def __init__(self, doc) -> None:
        from docx.oxml.ns import qn

        self._qn = qn
        self._counters: Dict[Tuple[str, str], int] = {}
        try:
            numbering = doc.part.numbering_part.element
        except (KeyError, NotImplementedError, AttributeError):
            numbering = None
        if numbering is None:
            self._abstract, self._nums = {}, {}
            return
        self._abstract = {
            an.get(qn("w:abstractNumId")): an
            for an in numbering.findall(qn("w:abstractNum"))
        }
        self._nums = {n.get(qn("w:numId")): n for n in numbering.findall(qn("w:num"))}

    def _level(self, num_id: str, ilvl: str):
        qn = self._qn
        num = self._nums.get(num_id)
        if num is None:
            return None, 1
        start_override = None
        for ov in num.findall(qn("w:lvlOverride")):
            if ov.get(qn("w:ilvl")) == ilvl:
                so = ov.find(qn("w:startOverride"))
                if so is not None:
                    start_override = int(so.get(qn("w:val")))
        abs_ref = num.find(qn("w:abstractNumId"))
        abs_el = self._abstract.get(abs_ref.get(qn("w:val"))) if abs_ref is not None else None
        if abs_el is None:
            return None, 1
        for lvl in abs_el.findall(qn("w:lvl")):
            if lvl.get(qn("w:ilvl")) == ilvl:
                start_el = lvl.find(qn("w:start"))
                start = int(start_el.get(qn("w:val"))) if start_el is not None else 1
                return lvl, start_override if start_override is not None else start
        return None, 1

    def restart(self) -> None:
        """Новый вопрос — списки заново с начала.

        У списка, заданного стилем, один номер на весь документ, и Word
        продолжает счёт: варианты второго вопроса выходили бы 6)–10),
        а разбор вариантов понимает только 1–5. Преподаватель видит у себя
        то же самое, но думает о вариантах вопроса, а не о сквозном счёте.
        """
        self._counters.clear()

    @staticmethod
    def _num_pr(paragraph):
        """Список задан у абзаца или у его стиля («Нумерованный список»)."""
        p_pr = paragraph._p.pPr
        if p_pr is not None and p_pr.numPr is not None:
            return p_pr.numPr
        try:
            style = paragraph.style
        except (KeyError, ValueError, AttributeError):
            return None
        depth = 0
        while style is not None and depth < 10:
            s_pr = style.element.pPr
            if s_pr is not None and s_pr.numPr is not None:
                return s_pr.numPr
            style = style.base_style
            depth += 1
        return None

    def label(self, paragraph) -> str:
        """Номер абзаца в списке («1)», «а.») или "", если он не в списке."""
        qn = self._qn
        num_pr = self._num_pr(paragraph)
        if num_pr is None or num_pr.numId is None:
            return ""
        num_id = str(num_pr.numId.val)
        if num_id == "0":  # «нумерация снята» у абзаца со списочным стилем
            return ""
        ilvl = str(num_pr.ilvl.val) if num_pr.ilvl is not None else "0"
        lvl, start = self._level(num_id, ilvl)
        if lvl is None:
            return ""

        key = (num_id, ilvl)
        self._counters[key] = self._counters[key] + 1 if key in self._counters else start
        # Вложенные уровни после возврата на верхний начинаются заново
        for other in [k for k in self._counters if k[0] == num_id and int(k[1]) > int(ilvl)]:
            del self._counters[other]

        fmt_el = lvl.find(qn("w:numFmt"))
        text_el = lvl.find(qn("w:lvlText"))
        value = _list_label(
            fmt_el.get(qn("w:val")) if fmt_el is not None else "", self._counters[key]
        )
        pattern = text_el.get(qn("w:val")) if text_el is not None else ""
        placeholder = f"%{int(ilvl) + 1}"
        if value is None or placeholder not in pattern:
            return ""
        return pattern.replace(placeholder, value)


def _paragraph_lines(text: str, label: str, keep_tabs: bool) -> List[str]:
    """Строки одного абзаца: номер списка впереди, перенос строки — граница.

    Перенос строки внутри абзаца (Shift+Enter) на экране — новая строка, и
    разбирать его надо так же. Иначе «…теорией / Ответ 2» в одном абзаце
    склеивалось в текст последнего варианта.
    """
    text = text or ""
    if label and text.strip():
        text = f"{label} {text.lstrip()}"
    out: List[str] = []
    for t in text.split("\n"):
        t = t.strip()
        if not keep_tabs:
            t = re.sub(r"\t+", " ", t)
        if t:
            out.append(t)
    return out


# Ячейка таблицы, где стоит только ответ «А2Б4В5», — это ключ, забытый
# в таблице задания. Показать его ученику значит выдать ответ.
_table_answer_cell_re = re.compile(r"^(?:[АБВГДЕABCDE]\d){2,}$")


def _table_lines(table, numbering: "_ListNumbering", keep_tabs: bool) -> List[str]:
    """Текст таблицы строками — по столбцам, сверху вниз.

    Задания «Установите соответствие» и «Вставьте в текст» набирают
    таблицей: слева А) Б) В), справа 1) 2) 3). Читать надо по столбцам:
    построчно выходило бы «А) … 1) … Б) … 2) …» вперемешку, а в «Вставьте»
    варианты разложены тройками 1-3-5 / 2-4-6 и по строкам шли бы не по
    порядку. Объединённая ячейка (текст задания над вариантами) берётся
    один раз.
    """
    grid = [list(row.cells) for row in table.rows]
    tcs = [[cell._tc for cell in row] for row in grid]

    def first_seen(r: int, c: int) -> bool:
        """Объединённая ячейка повторяется в сетке — берём её первое место."""
        tc = tcs[r][c]
        for rr in range(len(tcs)):
            for cc in range(len(tcs[rr])):
                if tcs[rr][cc] is tc:
                    return (rr, cc) == (r, c)
        return True

    # Номера автосписков считаются в порядке документа, то есть построчно,
    # и только потом ячейки переставляются по столбцам.
    cell_lines: Dict[Tuple[int, int], List[str]] = {}
    for r, row in enumerate(grid):
        for c, cell in enumerate(row):
            if not first_seen(r, c):
                continue
            lines: List[str] = []
            for p in cell.paragraphs:
                lines.extend(_paragraph_lines(p.text, numbering.label(p), keep_tabs))
            cell_lines[(r, c)] = lines

    out: List[str] = []
    width = max((len(r) for r in grid), default=0)
    for c in range(width):
        for r in range(len(grid)):
            for line in cell_lines.get((r, c), []):
                if not _table_answer_cell_re.match(line.replace(" ", "")):
                    out.append(line)
    return out


def _iter_docx_paragraphs(
    docx_path: Path, keep_tabs: bool = False, with_tables: bool = True
) -> List[str]:
    """Строки документа в порядке чтения — абзацы и, по желанию, таблицы.

    Таблица внутри текста — часть задания: у «Установите соответствие»
    в ней оба столбца. Без неё вопрос доходил до ученика одной строкой
    «Установите соответствие.» (клиент, 24.09.2026: 18 таких в одном файле).
    """
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    doc = Document(str(docx_path))
    numbering = _ListNumbering(doc)
    out: List[str] = []
    for child in doc.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            p = Paragraph(child, doc)
            if _question_marker_re.match(p.text or ""):
                numbering.restart()
            out.extend(_paragraph_lines(p.text, numbering.label(p), keep_tabs))
        elif tag == "tbl" and with_tables:
            out.extend(_table_lines(Table(child, doc), numbering, keep_tabs))
    return out


def _iter_docx_paragraphs_raw(docx_path: Path) -> List[str]:
    """Сохраняет табы — нужно для двухколоночного формата."""
    return _iter_docx_paragraphs(docx_path, keep_tabs=True)


# ---------- Ключ с ответами ----------

# Границы года. Всё, что похоже на год, сортировать нельзя ни при каких
# условиях: «1453» — дата падения Константинополя, а не выбор вариантов.
_YEAR_MIN, _YEAR_MAX = 1000, 2099


def _normalize_expected(raw: str) -> str:
    """Приводит ответ части В к каноническому виду.

    Три разных типа ответа, и путать их нельзя:
      • многовыбор «135» — порядок не важен, сортируем и убираем повторы;
      • последовательность/соответствие «БГВА», «А2Б3В1Г4» — порядок важен;
      • словесный ответ или год «Метрополия», «1795» — оставляем как есть.

    **Год проверяется первым.** Раньше многовыбором считались любые цифры
    из 1–5, и год «1453» превращался в «1345»: его цифры выглядят точно так
    же, как выбор вариантов 1, 4, 5 и 3. Различить их по самим цифрам нельзя,
    а по длине и диапазону — можно. Многовыбор, записанный по возрастанию,
    от сортировки не меняется, поэтому четырёхзначный выбор от этой проверки
    не страдает.
    """
    s = (raw or "").replace("\xa0", " ").strip()
    s = re.sub(r"\s+", " ", s).strip(" .,;:")
    if not s:
        return ""

    # Пробелы убираем только там, где они разделяют части одного ответа
    # («А2 Б3 В1» → «А2Б3В1»). В словесном ответе пробел — часть текста:
    # «Золотая Орда» не должна склеиться в «ЗолотаяОрда».
    compact = re.sub(r"\s+", "", s)
    upper = compact.upper().translate(_latin_to_cyr)

    if re.fullmatch(r"\d+", upper):
        if len(upper) == 4 and _YEAR_MIN <= int(upper) <= _YEAR_MAX:
            return compact

        # Многовыбор — цифры вариантов без повторов
        digits = list(upper)
        if (
            2 <= len(digits) <= 6
            and len(set(digits)) == len(digits)
            and all(d in "123456" for d in digits)
        ):
            return "".join(sorted(digits, key=int))
        return compact

    if re.fullmatch(r"[АБВГД]+", upper):
        return upper

    if re.fullmatch(r"(?:[АБВГД]\d){2,}", upper):
        return upper

    # Словесный ответ — сохраняем как написал преподаватель
    return s


def normalize_answer(raw: str) -> str:
    """Канонический вид ответа — публичное имя для `_normalize_expected`.

    Нужно проверке ответов ученика: она обязана приводить ответ к тому же
    виду, что и разбор файла, иначе «124» и «1, 2, 4» окажутся разными
    ответами. Второй такой функции быть не должно.
    """
    return _normalize_expected(raw)


# Маркеры частей пишут и кириллицей, и латиницей: «Часть А» / «Часть A»,
# «В1» / «B1». Здесь латинская B — это всегда В (по начертанию), не Б.
_PART_A_CHARS = "АA"
_PART_B_CHARS = "ВB"


def _extract_part_block(text: str, part_chars: str) -> str:
    pattern = (
        rf"Часть\s*[{part_chars}]\s*[:\-–—]\s*(.*?)"
        rf"(?=Часть\s*[{_PART_A_CHARS}{_PART_B_CHARS}]\s*[:\-–—]|$)"
    )
    m = re.search(pattern, text, flags=re.DOTALL | re.IGNORECASE)
    return (m.group(1) if m else "").strip()


# Пара «маркер — ответ»: «А1 — 2», «B7 — А3Б2В1», «В1 — 1453».
# Именно по ней узнаётся строка ключа, когда заголовка нет или он назван
# непривычно. Требуется тире или двоеточие после номера — без этого под
# описание попал бы любой вариант ответа вида «1) Рогволод».
#
# Регистр важен: маркер задания — всегда заглавная буква. Со строчной под
# описание попадал предлог: вариант «в 24-23 тыс. до н. э.; … в 22-21-м тыс.»
# давал две пары, строка считалась ключом, и всё ниже неё выпадало из
# разбора (клиент, 24.09.2026: из 80 вопросов части А нашлось 25).
_answer_pair_re = re.compile(
    rf"(?<![^\W\d_])[{_PART_A_CHARS}{_PART_B_CHARS}]\s*\d{{1,2}}\s*[-–—:]\s*\S",
)

# Сколько пар должно быть в строке, чтобы считать её ключом. Одна пара
# встречается в тексте вопроса («задание А1 — самое простое»), две подряд —
# уже не случайность.
_MIN_PAIRS_IN_KEY = 2


def looks_like_answer_key(line: str) -> bool:
    """Похожа ли строка на ключ с ответами — по форме, а не по заголовку.

    Запасной путь: заголовок ключа пишут как угодно («Эталоны», «Верные
    варианты», «Ключ к варианту 3»), и перечислять все написания бесполезно.
    А вот сама строка ключа выглядит одинаково всегда: маркер, тире, ответ,
    и так несколько раз подряд.
    """
    return len(_answer_pair_re.findall(line or "")) >= _MIN_PAIRS_IN_KEY


# Строка ключа начинается с ответа: «А1 — 2», «Часть В: В1 — 135».
# Именно «начинается» — этим она отличается от пояснения под ключом.
# В строке «А2Б3В1 — соответствие» пара «В1 — с» тоже есть, но стоит она
# в середине, и ответом на вопрос В1 эта строка не является.
_key_line_re = re.compile(
    rf"^\s*(?:Часть\s*[{_PART_A_CHARS}{_PART_B_CHARS}]\s*[:\-–—]?\s*)?"
    rf"[{_PART_A_CHARS}{_PART_B_CHARS}]\s*\d{{1,2}}\s*[-–—:]\s*\S",
    re.IGNORECASE,
)


def key_block(lines: List[str]) -> List[str]:
    """Строки ключа — до первой строки обычного текста после него.

    Раньше ключом считалось всё до конца файла, и подпись под ним ломала
    разбор: в пояснении «А2Б3В1 — соответствие» парсер вычитывал ответ
    на вопрос В1 и переписывал им настоящий ответ из ключа.

    Пустые строки и одинокие «Часть А:» ключ не обрывают — они внутри него
    встречаются. Обрывает первая же строка с обычным текстом, и только
    после того, как ответы уже начались: над ключом бывает подпись.

    Отдельно — перенос. В настоящих билетах длинный ключ не умещается
    в строку, и продолжение начинается с середины ответа: «А2Б3В4Г1;
    B11 — Волока; …». Начала-пары у такой строки нет, зато пар в ней много,
    а в подписи под ключом пара разве что случайная.
    """
    block: List[str] = []
    started = False
    for line in lines:
        text = (line or "").strip()
        if not text:
            block.append(line)
            continue
        if _key_line_re.match(text) or (started and looks_like_answer_key(text)):
            started = True
            block.append(line)
            continue
        if _part_boundary_re.match(text):
            block.append(line)
            continue
        if started:
            break
        block.append(line)
    return block


def _parse_answers(answer_lines: List[str]) -> Tuple[Dict[str, str], Dict[str, str]]:
    text = "\n".join(key_block(answer_lines))

    part_a_block = _extract_part_block(text, _PART_A_CHARS)
    part_b_block = _extract_part_block(text, _PART_B_CHARS)

    answers_a: Dict[str, str] = {}
    answers_b: Dict[str, str] = {}

    source_a = part_a_block or text
    for m in re.finditer(
        rf"[{_PART_A_CHARS}]\s*(\d+)\s*[-–—:]\s*(\d+)", source_a, flags=re.IGNORECASE
    ):
        answers_a[f"А{m.group(1)}"] = m.group(2)

    # Ответ тянется до следующего маркера «Вn —». Разделители между ответами
    # бывают разные: «В1 — Метрополия; В2 — ВАБ» и «В1 — А3Б2В1 В2 — БВГА».
    # Ограничение по маркеру, а не по разделителю, покрывает оба формата
    # и не режет словесные ответы.
    source_b = part_b_block or text
    for m in re.finditer(
        rf"[{_PART_B_CHARS}]\s*(\d+)\s*[-–—:]\s*(.+?)"
        rf"(?=\s*[{_PART_B_CHARS}]\s*\d+\s*[-–—:]|$)",
        source_b,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        answers_b[f"В{m.group(1)}"] = _normalize_expected((m.group(2) or "").strip())

    return answers_a, answers_b


# ---------- Разбор вопросов ----------

@dataclass(slots=True)
class ParsedQuestion:
    part: str  # А/В
    num: str
    question_text: str
    options: List[str]  # 5 слотов
    expected: str


def _split_line_by_numbers(line: str) -> List[Tuple[Optional[int], str]]:
    """Разбивает строку на фрагменты по маркерам N) или А)."""
    matches = list(_option_marker_re.finditer(line))

    if not matches:
        text = line.strip().rstrip(";.,").strip()
        return [(None, text)] if text else []

    result: List[Tuple[Optional[int], str]] = []

    prefix = line[: matches[0].start()].strip().rstrip(";.,\t ").strip()
    if prefix:
        result.append((None, prefix))

    for i, m in enumerate(matches):
        raw_marker = m.group(1)
        num = int(raw_marker) if raw_marker.isdigit() else _LETTER_TO_NUM.get(raw_marker, 0)
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(line)
        text = line[start:end].strip().rstrip(";.,\t ").strip()
        if 1 <= num <= 5 and text:
            result.append((num, text))

    return result


# Номер, набранный руками без скобки: «1 Хозяйство…», «1. Рим», «1<tab>Рим».
# После точки обязателен пробел: иначе «1.5 млн» потерял бы «1.» и превратился
# в «5 млн».
_LEADING_NUMBER_RE = re.compile(r"^(\d{1,2})(?:\.\s+|\s+)(\S.*)$")


def _strip_list_numbers(options: List[str]) -> List[str]:
    """Убирает ручную нумерацию вариантов: «1 Хозяйство…» → «Хозяйство…».

    Бот нумерует варианты сам, и номер из файла выходил вторым:
    «1️⃣ 1 Хозяйство…» (клиент, 17.09.2026). Срезаем, только если номера
    идут подряд с единицы у всех вариантов, — так вариант, который сам
    начинается с числа («1 сентября 1939 г.»), останется целым: у соседних
    вариантов такой лесенки не будет.
    """
    filled = [o for o in options if o]
    if len(filled) < MIN_OPTIONS:
        return options

    stripped: List[str] = []
    for expected, option in enumerate(filled, start=1):
        match = _LEADING_NUMBER_RE.match(option)
        if not match or int(match.group(1)) != expected:
            return options
        stripped.append(match.group(2).strip())

    rest = iter(stripped)
    return [next(rest) if o else o for o in options]


def _extract_options_from_lines(lines: List[str]) -> Tuple[str, List[str]]:
    """Достаёт варианты ответов из строк после маркера вопроса."""
    if not lines:
        return "", [""] * 5

    opts_map: Dict[int, str] = {}
    unnumbered: List[str] = []
    # Текст над вариантами, который к ним не относится: перечень событий
    # в вопросе на последовательность («1) заселение… 2) появление…», а под
    # ним варианты «1) 2431 2) 3214»). Раньше варианты затирали перечень,
    # и ученик получал коды без того, к чему они относятся.
    preface: List[str] = []

    for ln in lines:
        for num, text in _split_line_by_numbers(ln):
            if num is not None and 1 <= num <= 5:
                if num == 1 and opts_map:
                    preface.extend(unnumbered)
                    preface.extend(f"{k}) {v}" for k, v in sorted(opts_map.items()))
                    opts_map, unnumbered = {}, []
                opts_map[num] = text
            elif text:
                unnumbered.append(text)

    if opts_map:
        opts = [""] * 5
        for idx, val in opts_map.items():
            opts[idx - 1] = val
        free_slots = [i for i in range(5) if not opts[i]]
        # Ненумерованного больше, чем пустых мест, — лишнее стоит первым и это
        # хвост вопроса, перенесённый на новую строку («…на территории
        # Беларуси / являлось(-ась):»). Раньше он молча пропадал.
        overflow = len(unnumbered) - len(free_slots)
        if overflow > 0:
            preface.extend(unnumbered[:overflow])
            unnumbered = unnumbered[overflow:]
        for i, val in enumerate(unnumbered):
            opts[free_slots[i]] = val
        return "\n".join(preface), opts

    if preface:
        unnumbered = preface + unnumbered

    q_extra = ""
    total = unnumbered
    if len(total) > 5:
        extra_count = len(total) - 5
        q_extra = "\n".join(total[:extra_count])
        total = total[extra_count:]

    opts = [""] * 5
    for i, v in enumerate(total[:5]):
        opts[i] = v

    return q_extra, _strip_list_numbers(opts)


def parse_paragraphs(docx_path: Path) -> Tuple[List[ParsedQuestion], Dict[str, str], Dict[str, str]]:
    """Основной разбор .docx: вопросы идут абзацами, ключ — после «Ответы»."""
    return parse_lines(_iter_docx_paragraphs_raw(docx_path))


def parse_lines(
    paragraphs_raw: List[str],
) -> Tuple[List[ParsedQuestion], Dict[str, str], Dict[str, str]]:
    """Разбор готовых строк: вопросы идут абзацами, ключ — после «Ответы».

    Отдельно от чтения файла намеренно: строки одинаково приходят из .docx
    и из PDF, и второй парсер для второго формата заводить незачем — иначе
    любая правка разбора чинилась бы дважды.
    """
    paragraphs = [re.sub(r"\t+", " ", t) for t in paragraphs_raw]

    answers_idx: Optional[int] = None
    answers_head = ""
    for i, t in enumerate(paragraphs):
        match = _answers_header_re.match(t)
        if not match:
            continue
        tail = (match.group("tail") or "").strip()
        if len(tail) > _ANSWERS_TAIL_LIMIT:
            continue
        answers_idx = i
        answers_head = tail
        break

    # Заголовка нет или он назван непривычно — ищем сам ключ по форме строки.
    # Перечислять все написания заголовка бесполезно, а строка ключа выглядит
    # одинаково всегда. Строка ключа остаётся в блоке ответов целиком: она
    # и есть ответы, а не подпись над ними.
    if answers_idx is None:
        for i, t in enumerate(paragraphs):
            if looks_like_answer_key(t):
                answers_idx = i
                answers_head = t
                break

    if answers_idx is None:
        main_lines = paragraphs[::]
        main_lines_raw = paragraphs_raw[::]
        answers_a: Dict[str, str] = {}
        answers_b: Dict[str, str] = {}
    else:
        main_lines = paragraphs[:answers_idx]
        main_lines_raw = paragraphs_raw[:answers_idx]
        rest = paragraphs[answers_idx + 1:]
        answers_a, answers_b = _parse_answers(
            ([answers_head] if answers_head else []) + rest
        )

    questions: List[ParsedQuestion] = []
    current_part: Optional[str] = None
    current_num: Optional[str] = None
    current_marker_text: str = ""
    raw_lines_after_marker: List[str] = []
    block_lines: List[str] = []
    # «Ответ …» под вопросом. Если в файле есть и ключ в конце, прав ключ:
    # так было до появления ответов под вопросом, и так решено 24.09.2026.
    current_inline: Optional[str] = None

    def expected_for(key_answers: Dict[str, str], key: str) -> str:
        from_key = key_answers.get(key, "")
        if from_key or not current_inline:
            return from_key
        inline = current_inline
        # «1, 3, 5» под вопросом пишут чаще, чем «135» в ключе
        if re.fullmatch(r"\d(?:\s*[,;]\s*\d)+", inline):
            inline = re.sub(r"[\s,;]", "", inline)
        return _normalize_expected(inline)

    def flush() -> None:
        nonlocal current_part, current_num, current_marker_text
        nonlocal raw_lines_after_marker, block_lines, current_inline

        if not current_part or not current_num:
            current_inline = None
            return

        if current_part == "А":
            full_marker = current_marker_text.strip()
            if _no_split_re.search(full_marker):
                # Соответствие/последовательность — не дробим на варианты
                all_lines = [full_marker] if full_marker else []
                for ln in raw_lines_after_marker:
                    clean = re.sub(r"\t+", " ", ln).strip()
                    if clean:
                        all_lines.append(clean)
                q_text = "\n".join(all_lines).strip()
                opts = ["", "", "", "", ""]
            else:
                q_extra, opts = _extract_options_from_lines(raw_lines_after_marker)
                q_text = full_marker
                if q_extra:
                    q_text = q_text + "\n" + q_extra if q_text else q_extra
                opts = [o.rstrip(";.,").strip() for o in opts]

            questions.append(ParsedQuestion(
                part="А", num=current_num, question_text=q_text,
                options=opts, expected=expected_for(answers_a, f"А{current_num}"),
            ))
        else:
            lines = [current_marker_text.strip()] if current_marker_text.strip() else []
            lines.extend([x for x in block_lines if x.strip()])
            questions.append(ParsedQuestion(
                part="В", num=current_num, question_text="\n".join(lines).strip(),
                options=["", "", "", "", ""], expected=expected_for(answers_b, f"В{current_num}"),
            ))

        current_part = None
        current_num = None
        current_marker_text = ""
        raw_lines_after_marker = []
        block_lines = []
        current_inline = None

    for idx, line in enumerate(main_lines):
        line_raw = main_lines_raw[idx] if idx < len(main_lines_raw) else line

        if _page_header_re.match(line.strip()):
            continue

        if _part_boundary_re.match(line.strip()):
            flush()
            continue

        answer = inline_answer(line)
        if answer is not None:
            if current_part:
                current_inline = answer
            continue

        line, tail_answer = _split_answer_tail(line)
        if tail_answer:
            line_raw = _split_answer_tail(line_raw)[0] if _answer_tail_re.match(line_raw) else line

        m = _question_marker_re.match(line)
        if m:
            flush()
            raw_part = m.group(1).upper()
            current_part = {"A": "А", "B": "В", "а": "А", "в": "В", "b": "В", "a": "А"}.get(
                raw_part, raw_part
            )
            current_num = m.group(2)
            current_marker_text = m.group(3) or ""
            raw_lines_after_marker = []
            block_lines = []
            if tail_answer:
                current_inline = tail_answer
            continue

        if not current_part:
            continue

        if current_part == "А":
            raw_lines_after_marker.append(line_raw)
        else:
            block_lines.append(line)
        if tail_answer:
            current_inline = tail_answer

    flush()
    return questions, answers_a, answers_b


def _normalize_part_letter(raw: str) -> str:
    return {"A": "А", "B": "В", "a": "А", "b": "В"}.get(raw, raw)


def parse_tables(docx_path: Path) -> List[ParsedQuestion]:
    """Запасной разбор: вопросы лежат в таблицах Word (формат обобщений).

    Ключа с ответами в таком формате обычно нет — вопросы придут без «Ответа»
    и будут отсеяны фильтром качества. Это ожидаемо и сообщается преподавателю.
    """
    doc = Document(str(docx_path))
    if not doc.tables:
        return []

    questions: List[ParsedQuestion] = []

    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            if not cells:
                continue

            m = re.match(r"^\s*([АВABаваb])\s*(\d+)\s*$", cells[0].strip())
            if not m:
                continue

            part = _normalize_part_letter(m.group(1).upper())
            num = m.group(2)

            full_text = cells[-1].strip()
            if not full_text:
                continue

            lines = [ln for ln in full_text.split("\n") if ln.strip()]
            if not lines:
                continue

            if part != "А":
                questions.append(ParsedQuestion(
                    part="В", num=num, question_text="\n".join(lines).strip(),
                    options=["", "", "", "", ""], expected="",
                ))
                continue

            if _no_split_re.search(lines[0]):
                questions.append(ParsedQuestion(
                    part="А", num=num, question_text="\n".join(lines).strip(),
                    options=["", "", "", "", ""], expected="",
                ))
                continue

            q_lines: List[str] = []
            opt_lines: List[str] = []
            found_option = False
            for ln in lines:
                if not found_option and _option_marker_re.search(ln):
                    found_option = True
                (opt_lines if found_option else q_lines).append(ln)

            q_text = "\n".join(q_lines).strip() if q_lines else ""

            if opt_lines:
                opts_map: Dict[int, str] = {}
                unmarked: List[str] = []
                for ln in opt_lines:
                    matches = list(_option_marker_re.finditer(ln))
                    if matches:
                        pre = ln[: matches[0].start()].strip().rstrip(";.,\t ").strip()
                        if pre:
                            unmarked.append(pre)
                        for i_m, match in enumerate(matches):
                            raw = match.group(1)
                            n = int(raw) if raw.isdigit() else _LETTER_TO_NUM.get(raw, 0)
                            start = match.end()
                            end = matches[i_m + 1].start() if i_m + 1 < len(matches) else len(ln)
                            text = ln[start:end].strip().rstrip(";.,\t ").strip()
                            if 1 <= n <= 5 and text:
                                opts_map[n] = text
                    else:
                        clean = ln.strip().rstrip(";.,").strip()
                        if clean and not re.fullmatch(r"[1-5\s\)]+", clean):
                            unmarked.append(clean)

                opts = [opts_map.get(i, "").strip() for i in range(1, 6)]
                free_slots = [i for i in range(5) if not opts[i]]
                for i_u, val in enumerate(unmarked):
                    if i_u < len(free_slots):
                        opts[free_slots[i_u]] = val
            else:
                opts = ["", "", "", "", ""]

            # Маркеров не нашли: первая строка — вопрос, остальные — варианты
            if all(not o for o in opts) and len(lines) > 1:
                q_text = lines[0].strip()
                remaining = [ln.strip().rstrip(";.,") for ln in lines[1:] if ln.strip()]
                opts = [""] * 5
                for i_opt, val in enumerate(remaining[:5]):
                    opts[i_opt] = val

            questions.append(ParsedQuestion(
                part="А", num=num, question_text=q_text, options=opts, expected="",
            ))

    return questions


# ---------- Фильтр качества ----------

MIN_QUESTION_LEN = 10
MIN_OPTIONS = 2

REASON_SHORT_TEXT = "текст вопроса пустой или слишком короткий"
REASON_NO_ANSWER = "не найден правильный ответ"
REASON_BAD_ANSWER = "ответ не совпадает ни с одним вариантом"
REASON_FEW_OPTIONS = "меньше двух вариантов ответа"
REASON_LOST_OPTIONS = "ответ — номер варианта, но сами варианты не разобрались"


def validate(q: ParsedQuestion) -> Optional[str]:
    """Причина отбраковки или None, если вопрос пригоден.

    Тип вопроса определяется наличием вариантов, а не буквой части —
    так же, как это делают хендлеры тестов во время прохождения.
    """
    text = (q.question_text or "").strip()
    if len(text) < MIN_QUESTION_LEN:
        return REASON_SHORT_TEXT

    expected = (q.expected or "").strip()
    if not expected:
        return REASON_NO_ANSWER

    filled = [o for o in q.options if (o or "").strip()]

    if filled:
        # Вопрос с кнопками. Ответом может быть и несколько номеров сразу
        # («135» — многовыбор), движок тестов это поддерживает.
        if not re.fullmatch(r"[1-5]{1,5}", expected):
            return REASON_BAD_ANSWER
        if len(filled) < MIN_OPTIONS:
            return REASON_FEW_OPTIONS
        if any(not (q.options[int(d) - 1] or "").strip() for d in expected):
            return REASON_BAD_ANSWER
    elif q.part == "А" and re.fullmatch(r"[1-5]", expected):
        # У части А варианты обязаны быть. Если их нет, а ответ — номер
        # варианта, значит при разборе они потерялись: ученику не из чего
        # выбирать. Часть В сюда не попадает — там ответ вида «245» пишут
        # цифрами, а перечисление находится в самом тексте вопроса.
        return REASON_LOST_OPTIONS

    return None


def to_row(q: ParsedQuestion, variant: str, section: str = "") -> Dict[str, str]:
    opts = list(q.options) + [""] * (5 - len(q.options))
    return {
        "Вариант": variant,
        "Часть": q.part,
        "№": q.num,
        "Вопрос": q.question_text,
        "Вар.1": opts[0], "Вар.2": opts[1], "Вар.3": opts[2],
        "Вар.4": opts[3], "Вар.5": opts[4],
        "Ответ": q.expected,
        "Раздел": section,
    }


# ---------- Публичный интерфейс ----------

@dataclass
class ParseResult:
    variant: str
    rows: List[Dict[str, str]] = field(default_factory=list)
    rejected: List[Dict[str, str]] = field(default_factory=list)
    source: str = "none"  # paragraphs | tables | none

    @property
    def accepted_count(self) -> int:
        return len(self.rows)

    @property
    def rejected_count(self) -> int:
        return len(self.rejected)

    @property
    def total_found(self) -> int:
        return self.accepted_count + self.rejected_count

    def reasons_summary(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for r in self.rejected:
            out[r["reason"]] = out.get(r["reason"], 0) + 1
        return out

    def count_by_type(self) -> Tuple[int, int]:
        """(с вариантами ответа, с текстовым ответом).

        Тип определяется наличием вариантов — так же, как при прохождении теста.
        """
        with_options = sum(
            1 for r in self.rows if any(r[f"Вар.{i}"] for i in range(1, 6))
        )
        return with_options, len(self.rows) - with_options


def parse_docx(path: Path, variant: Optional[str] = None) -> ParseResult:
    """Разбирает один файл — .docx или PDF с текстом.

    Имя оставлено прежним: на него завязаны скрипты и вызовы в боте, а
    формат определяется по расширению, а не по названию функции.

    Разбор таблицами — запасной путь для формата обобщений, и он есть
    только у .docx: в PDF таблица это те же строки текста, их разбирает
    основной путь.
    """
    path = Path(path)
    variant = variant or path.stem
    result = ParseResult(variant=variant)

    if pdf_tools.is_pdf_name(path.name):
        questions, _a, _b = parse_lines(pdf_tools.extract_lines(path))
        source = "pdf" if questions else "none"
        return _finish(result, questions, source, variant, path)

    # Путь выбирается по абзацам без таблиц — как до того, как таблицы стали
    # читаться внутри текста. Иначе файл формата обобщений, где билет целиком
    # набран таблицей, ушёл бы в разбор абзацами вместо своего.
    plain, _a, _b = parse_lines(
        _iter_docx_paragraphs(path, keep_tabs=True, with_tables=False)
    )
    questions = parse_paragraphs(path)[0] if plain else []
    source = "paragraphs"

    if not questions:
        questions = parse_tables(path)
        source = "tables" if questions else "none"

    return _finish(result, questions, source, variant, path)


def _finish(
    result: ParseResult,
    questions: List[ParsedQuestion],
    source: str,
    variant: str,
    path: Path,
) -> ParseResult:
    """Фильтр качества и сборка строк — общая для всех форматов."""

    result.source = source

    for q in questions:
        reason = validate(q)
        if reason:
            result.rejected.append({
                "part": q.part,
                "num": q.num,
                "reason": reason,
                "question_text": (q.question_text or "")[:200],
            })
        else:
            result.rows.append(to_row(q, variant))

    logger.info(
        "parse %s: источник=%s принято=%d отбраковано=%d",
        path.name, source, result.accepted_count, result.rejected_count,
    )
    return result


def parse_many(paths: List[Path]) -> List[ParseResult]:
    results: List[ParseResult] = []
    for p in paths:
        try:
            results.append(parse_docx(p))
        except Exception:  # noqa: BLE001
            logger.exception("Не удалось разобрать %s", p)
            failed = ParseResult(variant=Path(p).stem)
            failed.rejected.append({
                "part": "", "num": "",
                "reason": "файл не удалось прочитать",
                "question_text": "",
            })
            results.append(failed)
    return results
