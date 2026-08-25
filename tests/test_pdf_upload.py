"""Приём PDF: чтение текста и честный отказ на сканах.

Поддержка PDF — это не второй парсер, а только превращение файла в список
строк: дальше работает тот же разбор, что и у .docx. Поэтому здесь
проверяется ровно две вещи — что строки достаются целыми и что скан
отличается от настоящего документа.

Разбор самих билетов из этих строк проверяется в test_ticket_parser.py.
"""

import shutil
import sys
import tempfile
from pathlib import Path

import pytest
from pdfminer.glyphlist import glyphname2unicode

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services import docx_tools, pdf_tools, ticket_parser  # noqa: E402


def make_pdf(lines, path: Path) -> Path:
    """Собирает настоящий PDF с текстовым слоем.

    Руками, а не библиотекой: тесту нужен файл, который читается как PDF,
    и тащить ради этого ещё одну зависимость незачем.

    Кириллица кладётся через таблицу имён глифов: базовая кодировка шрифта
    её не знает, поэтому каждому русскому символу выдаётся свой код и имя
    («А» → afii10017). Ровно так же устроены настоящие PDF, и без этого
    проверить разбор русского билета из файла было бы нечем.
    """
    reverse_glyphs = {char: name for name, char in glyphname2unicode.items()}

    codes: dict = {}
    differences: list = []

    def encode(char: str) -> str:
        if ord(char) < 128:
            return char
        if char not in codes:
            name = reverse_glyphs.get(char)
            if name is None:
                return "?"
            code = 128 + len(codes)
            codes[char] = code
            differences.append((code, name))
        return chr(codes[char])

    stream = "BT /F1 12 Tf 50 780 Td 16 TL\n"
    for line in lines:
        # В PDF скобки ограничивают строку, поэтому внутри они экранируются.
        # Выбрасывать их нельзя: «1)» — это номер варианта ответа.
        safe = line.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        stream += "(" + "".join(encode(ch) for ch in safe) + ") Tj T*\n"
    stream += "ET"

    diff_parts = " ".join(f"{code} /{name}" for code, name in differences)
    encoding = (
        f"/Encoding << /Type /Encoding /BaseEncoding /WinAnsiEncoding "
        f"/Differences [{diff_parts}] >>"
        if differences
        else "/Encoding /WinAnsiEncoding"
    )

    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        "/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        f"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica {encoding} >>",
        f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
    ]

    out = "%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{body}\nendobj\n"

    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n"
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_at}\n%%EOF"
    )

    path.write_bytes(out.encode("latin-1"))
    return path


@pytest.fixture
def pdf_dir():
    """Свой временный каталог: встроенный tmp_path здесь упирается в права."""
    path = Path(tempfile.mkdtemp(prefix="aida_pdf_"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


class TestReading:
    """Текст из PDF."""

    def test_lines_come_out_whole(self, pdf_dir):
        lines = [
            "A1. Who printed the first book and in which city did it happen?",
            "1) Skaryna in Prague and later in Vilnia together with others",
            "2) Turau in Polack during the same period of the century",
            "Answers: A1 - 1, A2 - 3, A3 - 2, A4 - 5, A5 - 4, A6 - 1",
        ]
        path = make_pdf(lines, pdf_dir / "ticket.pdf")

        assert pdf_tools.extract_lines(path) == lines

    def test_order_of_lines_is_kept(self, pdf_dir):
        """Порядок держит всё: маркер вопроса, его варианты и ключ идут подряд."""
        lines = [f"A{i}. Question number {i}" for i in range(1, 12)]
        path = make_pdf(lines, pdf_dir / "many.pdf")

        assert pdf_tools.extract_lines(path) == lines

    def test_blank_lines_are_dropped(self, pdf_dir):
        first = "A1. The first question of this ticket with enough words in it"
        second = "A2. The second question of the very same ticket, also long"
        path = make_pdf([first, "", "   ", second], pdf_dir / "gaps.pdf")

        assert pdf_tools.extract_lines(path) == [first, second]


class TestScans:
    """Скан отличается от документа — и об этом говорится прямо."""

    def test_page_without_text_is_refused(self, pdf_dir):
        """У скана страницы это картинки, букв в файле нет вовсе."""
        path = make_pdf([], pdf_dir / "scan.pdf")

        with pytest.raises(pdf_tools.PdfError) as exc:
            pdf_tools.extract_lines(path)
        assert "скан" in str(exc.value).lower()

    def test_broken_recognition_layer_is_refused(self, pdf_dir):
        """Слой распознавания бывает нечитаемым: текст рассыпан в огрызки.

        Настоящий случай: вместо «Прочитайте текст» из файла достаётся
        «дяежи а ч а ожани». Принять такое молча нельзя — преподаватель
        увидит «0 вопросов» и не поймёт, при чём тут он.
        """
        # Латиницей: в базовый шрифт PDF кириллица не кладётся, а проверяется
        # здесь форма текста, а не язык
        crumb_line = "d ya e zh i a ch a o zhani c Ee a nn a e shove i ll n p i E v e"
        path = make_pdf([crumb_line] * 4, pdf_dir / "ocr.pdf")

        with pytest.raises(pdf_tools.PdfError) as exc:
            pdf_tools.extract_lines(path)
        assert "нечитаемый" in str(exc.value).lower()

    def test_normal_text_is_not_mistaken_for_a_scan(self, pdf_dir):
        """Короткие слова есть и в живом тексте — предлоги, союзы, номера."""
        lines = [
            "A1. In which year did it happen and who was there at that time?",
            "1) in the year of the first union with a lot of small words here",
            "2) at the end of the war when it was all over for them",
            "Answers: A1 - 2, A2 - 1, A3 - 4, A4 - 3, A5 - 5",
        ]
        path = make_pdf(lines, pdf_dir / "fine.pdf")

        assert pdf_tools.extract_lines(path) == lines

    def test_broken_file_is_refused(self, pdf_dir):
        path = pdf_dir / "broken.pdf"
        path.write_bytes(b"%PDF-1.4 not really a pdf")

        with pytest.raises(pdf_tools.PdfError):
            pdf_tools.extract_lines(path)


class TestAccepted:
    """Какие файлы бот вообще берёт."""

    def test_both_formats_are_supported(self):
        assert docx_tools.is_supported_name("билеты.docx")
        assert docx_tools.is_supported_name("билеты.pdf")
        assert docx_tools.is_supported_name("БИЛЕТЫ.PDF")

    def test_other_formats_are_not(self):
        for name in ("скан.jpg", "фото.png", "старый.doc", "заметки.txt"):
            assert not docx_tools.is_supported_name(name), name

    def test_suffix_is_taken_from_the_name(self):
        assert docx_tools.suffix_of("Билет 1.PDF") == ".pdf"
        assert docx_tools.suffix_of("Билет 1.docx") == ".docx"
        assert docx_tools.suffix_of("фото.jpg") == ""

    def test_uploads_list_holds_both(self, pdf_dir):
        (pdf_dir / "a.docx").write_bytes(b"x")
        make_pdf(["text"], pdf_dir / "b.pdf")
        (pdf_dir / "c.jpg").write_bytes(b"x")

        names = [p.name for p in docx_tools.list_uploads(pdf_dir)]
        assert names == ["a.docx", "b.pdf"]


class TestParserEntry:
    """PDF доходит до общего разбора."""

    def _ticket_lines(self):
        lines = ["Часть А"]
        for num in range(1, 4):
            lines += [
                f"А{num}. Кто основал город в этом году и при каком князе?",
                "1) Рогволод и его дружина в те далёкие времена",
                "2) Всеслав Чародей вместе с полоцкими боярами",
                "3) Изяслав Владимирович по решению веча",
                "4) Брячислав Изяславич после долгого похода",
            ]
        lines += ["Ответы", "Часть А: А1 — 2; А2 — 1; А3 — 4"]
        return lines

    def test_russian_ticket_becomes_questions(self, pdf_dir):
        """Главная проверка: PDF на входе — готовые вопросы на выходе."""
        path = make_pdf(self._ticket_lines(), pdf_dir / "Билет 1.pdf")

        result = ticket_parser.parse_docx(path)

        assert result.source == "pdf"
        assert result.accepted_count == 3
        assert result.rejected_count == 0

    def test_answers_from_the_key_reach_the_rows(self, pdf_dir):
        """Без ключа вопросы не проходят фильтр — значит ключ прочитан."""
        path = make_pdf(self._ticket_lines(), pdf_dir / "Билет 2.pdf")

        rows = ticket_parser.parse_docx(path).rows

        assert [r["Ответ"] for r in rows] == ["2", "1", "4"]
        assert rows[0]["Вар.1"].startswith("Рогволод")
        assert rows[0]["Часть"] == "А"

    def test_scan_stops_before_the_parser(self, pdf_dir):
        """Ошибку видит хендлер и показывает её преподавателю целиком."""
        path = make_pdf([], pdf_dir / "scan.pdf")

        with pytest.raises(pdf_tools.PdfError):
            ticket_parser.parse_docx(path)

    def test_cyrillic_ticket_parses_from_plain_lines(self):
        """Строки из PDF идут в тот же разбор, что и абзацы Word.

        Здесь без файла: в базовый шрифт PDF кириллица не кладётся, а
        проверить надо именно разбор настоящего билета.
        """
        lines = [
            "Часть А",
            "А1. Кто основал город?",
            "1) Рогволод",
            "2) Всеслав",
            "3) Изяслав",
            "4) Брячислав",
            "А2. В каком году это произошло?",
            "1) 1067",
            "2) 1101",
            "3) 1132",
            "4) 1216",
            "Ответы",
            "Часть А: А1 — 2; А2 — 1",
        ]

        questions, answers_a, _ = ticket_parser.parse_lines(lines)

        assert len(questions) == 2
        assert answers_a == {"А1": "2", "А2": "1"}
        assert all(ticket_parser.validate(q) is None for q in questions)

    def test_docx_path_is_untouched(self):
        """Разбор Word не должен измениться от появления PDF."""
        samples = ROOT / "data/raw/tests"
        sample = samples / "Раннее Новое время.docx"
        if not sample.exists():
            pytest.skip("нет образцов билетов")

        result = ticket_parser.parse_docx(sample)
        assert result.source == "paragraphs"
        assert result.accepted_count > 0
