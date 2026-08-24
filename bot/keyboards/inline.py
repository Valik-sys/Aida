from __future__ import annotations

from typing import Dict, List, Sequence

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

import subjects as subjects_cfg
from database.models import ROLE_STUDENT, ROLE_TEACHER
from services import sections as sections_lib

# Названия разделов истории. Источник один — subjects.py, чтобы сетка
# программы не разъезжалась между экранами ученика и загрузкой билетов.
SECTION_NAMES = [
    s["full"] for s in subjects_cfg.subject_sections(subjects_cfg.DEFAULT_SUBJECT)
]

# Тексты кнопок главного меню — используются для фильтрации в хендлерах.
# Подписи режимов живут в subjects.MODE_LABELS, здесь только псевдонимы,
# чтобы не переписывать существующие хендлеры.
MENU_BTN_TESTS = subjects_cfg.MODE_LABELS["tests"]
MENU_BTN_FLASHCARDS = subjects_cfg.MODE_LABELS["flashcards"]
MENU_BTN_ASK = subjects_cfg.MODE_LABELS["ask"]
MENU_BTN_TOPICS = subjects_cfg.MODE_LABELS["topics"]
MENU_BTN_MISTAKES = subjects_cfg.MODE_LABELS["mistakes"]
MENU_BTN_PROGRESS = subjects_cfg.MODE_LABELS["progress"]
MENU_BTN_HOME = "◀️ Меню"

# Кнопки преподавателя.
# «Загрузить билеты» не поняли двое преподавателей из трёх на созвонах —
# спрашивали, что именно сюда грузить. «Добавить вопросы» читается однозначно.
MENU_BTN_UPLOAD = "📥 Добавить вопросы"
MENU_BTN_MY_TRAINER = "👀 Мой тренажёр"
MENU_BTN_STUDENTS = "👥 Мои ученики"

# Переключение между кабинетом и тем, что видит ученик.
# Режим держится самой клавиатурой: она остаётся у пользователя до замены,
# поэтому хранить состояние на сервере не нужно.
MENU_BTN_SETTINGS = "⚙️ Настройки"
MENU_BTN_STUDENT_VIEW = "🎓 Режим ученика"
MENU_BTN_BACK_TO_CABINET = "⬅️ В кабинет"

# Все подписи кнопок нижней клавиатуры. Нужны, чтобы режимы, ожидающие ввода,
# не проглатывали нажатия меню: иначе из такого режима не выйти.
ALL_MENU_BUTTONS = frozenset({
    MENU_BTN_TESTS, MENU_BTN_FLASHCARDS, MENU_BTN_ASK,
    MENU_BTN_TOPICS, MENU_BTN_MISTAKES, MENU_BTN_PROGRESS, MENU_BTN_HOME,
    MENU_BTN_UPLOAD, MENU_BTN_MY_TRAINER, MENU_BTN_STUDENTS,
    MENU_BTN_SETTINGS, MENU_BTN_STUDENT_VIEW, MENU_BTN_BACK_TO_CABINET,
})

# Как режимы раскладываются по рядам клавиатуры: сначала пары, потом одиночки.
_MODE_ROWS = [
    ["topics"],
    ["tests", "flashcards"],
    ["ask"],
    ["mistakes", "progress"],
]


def _mode_keyboard_rows(subject: str) -> List[List[KeyboardButton]]:
    """Ряды кнопок из режимов, включённых у предмета. Скрытые не попадают."""
    rows: List[List[KeyboardButton]] = []
    for row in _MODE_ROWS:
        buttons = [
            KeyboardButton(text=subjects_cfg.MODE_LABELS[m])
            for m in row
            if subjects_cfg.is_mode_enabled(subject, m)
        ]
        if buttons:
            rows.append(buttons)
    return rows


def teacher_cabinet_kb(subject: str | None = None) -> ReplyKeyboardMarkup:
    """Кабинет преподавателя: только настройка тренажёра.

    Режимы занятий сюда не попадают — за ними отдельная кнопка, иначе
    настройка и тренировка смешиваются в одну кучу.
    """
    subject = subject or subjects_cfg.DEFAULT_SUBJECT
    rows: List[List[KeyboardButton]] = []
    if subjects_cfg.teacher_can_upload(subject, "tests"):
        rows.append([KeyboardButton(text=MENU_BTN_UPLOAD)])
    rows.append(
        [KeyboardButton(text=MENU_BTN_MY_TRAINER), KeyboardButton(text=MENU_BTN_STUDENTS)]
    )
    rows.append(
        [KeyboardButton(text=MENU_BTN_SETTINGS), KeyboardButton(text=MENU_BTN_STUDENT_VIEW)]
    )
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def trainer_kb(
    has_files: bool = False,
    has_sections: bool = False,
    has_reports: bool = False,
) -> InlineKeyboardMarkup:
    """Экран тренажёра: опись загруженного и быстрый переход к загрузке."""
    rows: List[List[InlineKeyboardButton]] = []
    top: List[InlineKeyboardButton] = []
    if has_sections:
        top.append(InlineKeyboardButton(text="🗂 Разделы", callback_data="trainer:sections"))
    if has_files:
        top.append(InlineKeyboardButton(text="📎 Файлы", callback_data="trainer:files"))
    if top:
        rows.append(top)
    # Отдельной строкой и только когда есть что разбирать: пустая кнопка
    # каждый день мозолила бы глаза
    if has_reports:
        rows.append([InlineKeyboardButton(
            text="⚠️ Спорные вопросы", callback_data="trainer:reports"
        )])
    rows.append([InlineKeyboardButton(
        text="📥 Добавить вопросы", callback_data="trainer:upload"
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_to_trainer_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data="trainer:root")]
        ]
    )


def questions_word(n: int) -> str:
    """«1 вопрос», «2 вопроса», «5 вопросов»."""
    if 11 <= n % 100 <= 14:
        return "вопросов"
    return {1: "вопрос", 2: "вопроса", 3: "вопроса", 4: "вопроса"}.get(n % 10, "вопросов")


# ---------- Разделы преподавателя ----------

SEC_PREFIX = "sec"

# Псевдораздел для вопросов, которым раздел не назначен: полные билеты,
# где разделы перемешаны. Ключ уходит в callback, название — на экран,
# и оно одно и то же у преподавателя при загрузке и у ученика в списке.
UNSORTED_KEY = "none"
UNSORTED_LABEL = sections_lib.UNSORTED_TITLE


def section_confirm_kb(key: str, label: str) -> InlineKeyboardMarkup:
    """Подтверждение подсказанного раздела.

    Подсказка стоит первой кнопкой, но согласие всегда явное: угадали мы
    по имени файла, а отвечает за раскладку преподаватель.
    """
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"✅ {label}", callback_data=f"{SEC_PREFIX}:{key}")],
        [InlineKeyboardButton(text="Выбрать другой раздел", callback_data=f"{SEC_PREFIX}:list")],
        [InlineKeyboardButton(
            text=UNSORTED_LABEL, callback_data=f"{SEC_PREFIX}:{UNSORTED_KEY}"
        )],
    ])


def sections_kb_for_upload(
    sections: Sequence,
    *,
    allow_none: bool = True,
    none_text: str = UNSORTED_LABEL,
) -> InlineKeyboardMarkup:
    """Список разделов: программа, свои и «готовые варианты».

    Последний пункт — не отказ от выбора, а самостоятельное место: туда
    кладут полные билеты, в которых вопросы разных разделов вперемешку.
    """
    rows: List[List[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text=s.label, callback_data=f"{SEC_PREFIX}:{s.key}")]
        for s in sections
    ]
    rows.append([InlineKeyboardButton(
        text="➕ Свой раздел", callback_data=f"{SEC_PREFIX}:new"
    )])
    if allow_none:
        rows.append([InlineKeyboardButton(
            text=none_text, callback_data=f"{SEC_PREFIX}:{UNSORTED_KEY}"
        )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


TOP_PREFIX = "top"

# «Весь раздел» — не отказ от темы, а законный выбор: файл с полным билетом
# по разделу темой не разложить, а место ему нужно.
WHOLE_SECTION_KEY = "whole"
WHOLE_SECTION_LABEL = "📚 Весь раздел, без темы"


def topics_kb_for_upload(
    topics: Sequence,
    *,
    allow_back: bool = True,
) -> InlineKeyboardMarkup:
    """Темы внутри выбранного раздела.

    Подписи режутся по границе фразы: названия тем в программе — это
    перечисления через точку, целиком они превращают клавиатуру в стену.
    """
    rows: List[List[InlineKeyboardButton]] = [
        [InlineKeyboardButton(
            text=sections_lib.button_label(t.title),
            callback_data=f"{TOP_PREFIX}:{t.key}",
        )]
        for t in topics
    ]
    rows.append([InlineKeyboardButton(
        text="➕ Новая тема", callback_data=f"{TOP_PREFIX}:new"
    )])
    rows.append([InlineKeyboardButton(
        text=WHOLE_SECTION_LABEL,
        callback_data=f"{TOP_PREFIX}:{WHOLE_SECTION_KEY}",
    )])
    if allow_back:
        rows.append([InlineKeyboardButton(
            text="← Назад к разделам", callback_data=f"{TOP_PREFIX}:back"
        )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def uploaded_kb(
    section_label: str | None = None,
    file_token: str | None = None,
) -> InlineKeyboardMarkup:
    """Что можно сделать сразу после загрузки файла.

    В кнопке «Другой раздел» ярлык файла: без него она перекладывала бы
    тот файл, который загружали последним, а не тот, под чьим отчётом
    её нажали.
    """
    rows: List[List[InlineKeyboardButton]] = []
    if section_label and file_token:
        rows.append([InlineKeyboardButton(
            text="Другой раздел", callback_data=f"{SEC_PREFIX}:list:{file_token}"
        )])
    rows.append([InlineKeyboardButton(text="👀 Мой тренажёр", callback_data="trainer:root")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def trainer_sections_kb(has_unsorted: bool = False) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    if has_unsorted:
        rows.append([InlineKeyboardButton(
            text="🗂 Разложить по разделам", callback_data="trainer:sort"
        )])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="trainer:root")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def settings_kb() -> InlineKeyboardMarkup:
    """Раздел настроек. Пока одна, дальше будут добавляться сюда же."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📚 Материалы тренажёра", callback_data="settings:source")],
        ]
    )


def source_choice_kb(subject: str, current: str) -> InlineKeyboardMarkup:
    """Выбор источника материалов. Текущий помечен галочкой."""
    rows: List[List[InlineKeyboardButton]] = []
    for value in subjects_cfg.allowed_sources(subject):
        mark = "✓ " if value == current else ""
        rows.append([InlineKeyboardButton(
            text=f"{mark}{subjects_cfg.SOURCE_LABELS[value]}",
            callback_data=f"settings:source:{value}",
        )])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="settings:root")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def student_view_kb(subject: str | None = None, *, for_teacher: bool = False) -> ReplyKeyboardMarkup:
    """Режимы занятий. Преподавателю добавляется возврат в кабинет."""
    subject = subject or subjects_cfg.DEFAULT_SUBJECT
    rows = _mode_keyboard_rows(subject)
    if for_teacher:
        rows.append([KeyboardButton(text=MENU_BTN_BACK_TO_CABINET)])
    if not rows:
        rows = [[KeyboardButton(text=MENU_BTN_HOME)]]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def main_menu_kb(subject: str | None = None, role: str | None = None) -> ReplyKeyboardMarkup:
    """Клавиатура по умолчанию: преподавателю — кабинет, ученику — режимы."""
    if role == ROLE_TEACHER:
        return teacher_cabinet_kb(subject)
    return student_view_kb(subject)


# ---------- Онбординг: роль и предмет ----------

def mode_help_kb(mode: str) -> InlineKeyboardMarkup:
    """Кнопка с пояснением режима — чтобы не грузить им экран входа."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Как это работает", callback_data=f"help:{mode}")]
        ]
    )


# Сколько учеников показывать на одной странице списка
STUDENTS_PAGE_SIZE = 8


def students_page_count(total: int) -> int:
    if total <= 0:
        return 1
    return (total + STUDENTS_PAGE_SIZE - 1) // STUDENTS_PAGE_SIZE


def clamp_page(page: int, total: int) -> int:
    """Страница всегда в границах — например, после удаления учеников."""
    return max(0, min(page, students_page_count(total) - 1))


def students_kb(students: Sequence, page: int = 0) -> InlineKeyboardMarkup:
    """Список учеников кнопками, по странице за раз.

    При большом классе кнопки нельзя вываливать пластом — экран становится
    нечитаемым, а Telegram всё равно ограничивает размер клавиатуры.
    """
    total = len(students)
    page = clamp_page(page, total)
    start = page * STUDENTS_PAGE_SIZE
    chunk = students[start:start + STUDENTS_PAGE_SIZE]

    rows: List[List[InlineKeyboardButton]] = [
        [InlineKeyboardButton(
            text=s.name or f"Ученик {s.telegram_id}",
            callback_data=f"student:{s.telegram_id}:{page}",
        )]
        for s in chunk
    ]

    pages = students_page_count(total)
    if pages > 1:
        nav: List[InlineKeyboardButton] = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="◀", callback_data=f"students:page:{page - 1}"))
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="noop"))
        if page < pages - 1:
            nav.append(InlineKeyboardButton(text="▶", callback_data=f"students:page:{page + 1}"))
        rows.append(nav)

    return InlineKeyboardMarkup(inline_keyboard=rows)


def student_card_kb(page: int = 0) -> InlineKeyboardMarkup:
    """Возврат к списку — на ту же страницу, с которой открыли."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="◀️ К списку", callback_data=f"students:list:{page}")]
        ]
    )


def back_to_mode_kb(mode: str) -> InlineKeyboardMarkup:
    """Возврат к выбору внутри режима после прочтения пояснения."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data=f"open:{mode}")]
        ]
    )


def explain_kb(idx: int) -> InlineKeyboardMarkup:
    """Кнопка разбора ошибки по материалам.

    Разбор по запросу, а не автоматически: он стоит обращения к модели,
    и нужен далеко не после каждой ошибки.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text="🔍 Разобрать по материалам",
                callback_data=f"tests:explain:{idx}",
            )]
        ]
    )


def role_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👩‍🏫 Я преподаватель", callback_data=f"role:{ROLE_TEACHER}")],
            [InlineKeyboardButton(text="🎓 Я ученик", callback_data=f"role:{ROLE_STUDENT}")],
        ]
    )


def subjects_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=subjects_cfg.subject_title(key), callback_data=f"subject:{key}")]
        for key in subjects_cfg.all_subject_keys()
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def tests_root_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎯 Полный билет · 38 вопросов", callback_data="tests:ticket")],
            [InlineKeyboardButton(text="✍️ Только часть Б", callback_data="tests:part_b")],
            [InlineKeyboardButton(text="🗂 По разделу", callback_data="tests:by_section")],
            [InlineKeyboardButton(text="📚 Тесты прошлых лет", callback_data="tests:archive")],
            [
                InlineKeyboardButton(text="Как это работает", callback_data="help:tests"),
                InlineKeyboardButton(text=MENU_BTN_HOME, callback_data="menu:back_to_main"),
            ],
        ]
    )


def sections_kb(sections: Sequence) -> InlineKeyboardMarkup:
    """Разделы, по которым можно тренироваться: список пар (ключ, подпись).

    Список приходит готовым и содержит только непустые разделы. Иначе
    ученик у преподавателя с двумя загруженными файлами тыкал бы в восемь
    разделов и семь раз получал «вопросов нет».
    """
    rows: List[List[InlineKeyboardButton]] = []

    for key, label in sections:
        rows.append([InlineKeyboardButton(
            text=label, callback_data=f"tests:section:{key}"
        )])

    rows.append([InlineKeyboardButton(text=MENU_BTN_HOME, callback_data="menu:back_to_main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def student_topics_kb(topics: Sequence, section_key: str) -> InlineKeyboardMarkup:
    """Темы внутри раздела: список троек (ключ, подпись, сколько вопросов).

    «Весь раздел целиком» стоит последним, а не первым: ученик пришёл сюда
    за темой, которую разобрали на занятии, и первым должно быть то, за чем
    он пришёл. Пустые темы в список не попадают.
    """
    rows: List[List[InlineKeyboardButton]] = []

    for key, label, count in topics:
        rows.append([InlineKeyboardButton(
            text=f"{sections_lib.button_label(label)} · {count}",
            callback_data=f"tests:section:{key}",
        )])

    # Отдельный префикс, а не «tests:section:6»: тот снова открыл бы список тем,
    # и «весь раздел» превратился бы в кольцо.
    rows.append([InlineKeyboardButton(
        text="▶️ Весь раздел целиком",
        callback_data=f"tests:secall:{section_key}",
    )])
    rows.append([InlineKeyboardButton(
        text="← Назад к разделам", callback_data="tests:by_section"
    )])
    rows.append([InlineKeyboardButton(text=MENU_BTN_HOME, callback_data="menu:back_to_main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def variants_kb(variant_labels: Sequence[str]) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    for idx, label in enumerate(variant_labels):
        rows.append([InlineKeyboardButton(text=label, callback_data=f"tests:variant_idx:{idx}")])
    rows.append([InlineKeyboardButton(text=MENU_BTN_HOME, callback_data="menu:back_to_main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def quantity_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="5", callback_data="tests:qty:5"),
                InlineKeyboardButton(text="10", callback_data="tests:qty:10"),
            ],
            [
                InlineKeyboardButton(text="20", callback_data="tests:qty:20"),
                InlineKeyboardButton(text="все", callback_data="tests:qty:all"),
            ],
            [InlineKeyboardButton(text=MENU_BTN_HOME, callback_data="menu:back_to_main")],
        ]
    )


def test_part_a_kb(qidx: int, options: Dict[int, str]) -> InlineKeyboardMarkup:
    # В части А по ТЗ 5 вариантов ответа.
    rows: List[List[InlineKeyboardButton]] = []
    for i in range(1, 6):
        text = options.get(i, "").strip()
        label = f"{i}) {text}" if text else str(i)
        rows.append([InlineKeyboardButton(text=label, callback_data=f"test:ansA:{qidx}:{i}")])
    rows.append([InlineKeyboardButton(text=MENU_BTN_HOME, callback_data="menu:back_to_main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------- Жалоба на вопрос ----------

REPORT_PREFIX = "qrep"
REASON_PREFIX = "qrsn"
REPORT_CARD_PREFIX = "rep"


def short_text(text: str, limit: int = 120) -> str:
    """Однострочный кусочек вопроса. Без экранирования — для сообщений без разметки."""
    flat = " ".join((text or "").split())
    if len(flat) > limit:
        return flat[:limit].rstrip(" ,.;:") + "…"
    return flat


def reported_open_kb() -> InlineKeyboardMarkup:
    """Из уведомления — сразу в разбор."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Посмотреть", callback_data="trainer:reports")]
    ])


def report_card_kb(hidden: bool) -> InlineKeyboardMarkup:
    """Карточка спорного вопроса. Действия зависят от того, скрыт ли он."""
    if hidden:
        first = [
            InlineKeyboardButton(
                text="Вернуть в тренажёр", callback_data=f"{REPORT_CARD_PREFIX}:ok"
            ),
            InlineKeyboardButton(
                text="Убрать совсем", callback_data=f"{REPORT_CARD_PREFIX}:remove"
            ),
        ]
    else:
        first = [
            InlineKeyboardButton(
                text="Скрыть", callback_data=f"{REPORT_CARD_PREFIX}:remove"
            ),
            InlineKeyboardButton(
                text="Всё в порядке", callback_data=f"{REPORT_CARD_PREFIX}:ok"
            ),
        ]
    return InlineKeyboardMarkup(inline_keyboard=[
        first,
        [
            InlineKeyboardButton(text="⏭ Позже", callback_data=f"{REPORT_CARD_PREFIX}:later"),
            InlineKeyboardButton(text="◀️ Назад", callback_data="trainer:root"),
        ],
    ])


def question_report_kb(qhash: str) -> InlineKeyboardMarkup:
    """Единственная кнопка под вопросом. Ответ ученик по-прежнему пишет текстом.

    В кнопке отпечаток вопроса, а не его номер в наборе: клавиатуры старых
    сообщений живут в чате вечно, и по номеру жалоба попадала бы в тот
    вопрос, который случайно оказался на этом месте в текущем тесте.
    """
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="⚠️ Что-то не так", callback_data=f"{REPORT_PREFIX}:{qhash}"
        )]
    ])


def report_reasons_kb(qhash: str, reasons: Sequence) -> InlineKeyboardMarkup:
    """Причины жалобы. Заменяют кнопку под тем же вопросом, текст остаётся на месте."""
    rows = [
        [InlineKeyboardButton(
            text=label, callback_data=f"{REASON_PREFIX}:{qhash}:{key}"
        )]
        for key, label in reasons
    ]
    rows.append([InlineKeyboardButton(
        text="Отмена", callback_data=f"{REPORT_PREFIX}:c:{qhash}"
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def test_result_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Заново", callback_data="test:restart")],
            [InlineKeyboardButton(text="📝 Другой тест", callback_data="test:other")],
            [InlineKeyboardButton(text=MENU_BTN_HOME, callback_data="menu:back_to_main")],
        ]
    )


def flashcards_root_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🕰 Даты", callback_data="flash:cat:dates")],
            [InlineKeyboardButton(text="🧩 Понятия", callback_data="flash:cat:concepts")],
            [InlineKeyboardButton(text="🎭 Личности", callback_data="flash:cat:persons")],
            [InlineKeyboardButton(text="🔀 Всё вперемешку", callback_data="flash:cat:mixed")],
            [
                InlineKeyboardButton(text="Как это работает", callback_data="help:flashcards"),
                InlineKeyboardButton(text=MENU_BTN_HOME, callback_data="menu:back_to_main"),
            ],
        ]
    )


def flashcards_section_kb(sections: Sequence[str] | None = None) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    rows.append([InlineKeyboardButton(text="Все разделы", callback_data="flash:section:all")])
    nums = sections if sections else [str(n) for n in range(1, 9)]
    for s in nums:
        try:
            name = SECTION_NAMES[int(s) - 1]
        except (ValueError, IndexError):
            name = f"Раздел {s}"
        rows.append([InlineKeyboardButton(text=name, callback_data=f"flash:section:{s}")])
    rows.append([InlineKeyboardButton(text=MENU_BTN_HOME, callback_data="menu:back_to_main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def flashcards_reveal_kb(card_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👁️ Показать ответ", callback_data=f"flash:reveal:{card_id}")],
            [InlineKeyboardButton(text=MENU_BTN_HOME, callback_data="menu:back_to_main")],
        ]
    )


def flashcards_action_kb(card_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🎯 Знаю", callback_data=f"flash:mark:know:{card_id}"),
                InlineKeyboardButton(text="🔻 Не знаю", callback_data=f"flash:mark:unknown:{card_id}"),
            ],
            [InlineKeyboardButton(text="Закончить", callback_data="flash:finish")],
            [InlineKeyboardButton(text=MENU_BTN_HOME, callback_data="menu:back_to_main")],
        ]
    )


def topics_entry_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📚 Выбрать раздел", callback_data="topics:start")],
            [InlineKeyboardButton(text=MENU_BTN_HOME, callback_data="menu:back_to_main")],
        ]
    )


def topics_sections_kb(sections: list[str]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=s, callback_data=f"topics:section:{i}")]
        for i, s in enumerate(sections)
    ]
    rows.append([InlineKeyboardButton(text=MENU_BTN_HOME, callback_data="menu:back_to_main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def topics_subtopics_kb(
    subtopics: list[str], section_idx: str, *, final_unlocked: bool = False
) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=s, callback_data=f"topics:subtopic:{section_idx}:{i}")]
        for i, s in enumerate(subtopics)
    ]
    if final_unlocked:
        rows.append([InlineKeyboardButton(
            text="🏆 Итоговый тест по разделу",
            callback_data=f"topics:final_test:{section_idx}",
        )])
    else:
        rows.append([InlineKeyboardButton(
            text="🔒 Итоговый тест (пройди все темы)",
            callback_data="topics:final_test_locked",
        )])
    rows.append([InlineKeyboardButton(text="← Разделы", callback_data="topics:start")])
    rows.append([InlineKeyboardButton(text=MENU_BTN_HOME, callback_data="menu:back_to_main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def topics_menu_kb(section_idx: str, subtopic_idx: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📖 Теория", callback_data=f"topics:theory:{section_idx}:{subtopic_idx}")],
            [InlineKeyboardButton(text="🎴 Карточки", callback_data=f"topics:cards:{section_idx}:{subtopic_idx}")],
            [InlineKeyboardButton(text="📝 Тест", callback_data=f"topics:test:{section_idx}:{subtopic_idx}")],
            [InlineKeyboardButton(text="💬 Задать вопрос", callback_data=f"topics:ask:{section_idx}:{subtopic_idx}")],
            [InlineKeyboardButton(text="← Темы", callback_data=f"topics:section:{section_idx}")],
            [InlineKeyboardButton(text=MENU_BTN_HOME, callback_data="menu:back_to_main")],
        ]
    )


def topic_final_test_question_kb(section_idx: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="← К разделу", callback_data=f"topics:section:{section_idx}")],
            [InlineKeyboardButton(text=MENU_BTN_HOME, callback_data="menu:back_to_main")],
        ]
    )


def final_test_result_kb(section_idx: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Новый тест", callback_data=f"topics:final_test:{section_idx}")],
            [InlineKeyboardButton(text="← К разделу", callback_data=f"topics:section:{section_idx}")],
            [InlineKeyboardButton(text=MENU_BTN_HOME, callback_data="menu:back_to_main")],
        ]
    )


def topic_test_question_kb(section_idx: str, subtopic_idx: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="← К теме", callback_data=f"topic_test:back:{section_idx}:{subtopic_idx}")],
            [InlineKeyboardButton(text=MENU_BTN_HOME, callback_data="menu:back_to_main")],
        ]
    )


def topic_test_resume_kb(section_idx: str, subtopic_idx: str, current: int, total: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"▶️ Продолжить (вопрос {current + 1} из {total})", callback_data="topic_test:resume")],
            [InlineKeyboardButton(text="🔄 Начать заново", callback_data="topic_test:fresh")],
            [InlineKeyboardButton(text="← К теме", callback_data=f"topics:subtopic:{section_idx}:{subtopic_idx}")],
        ]
    )


def topic_test_next_kb(section_idx: str, subtopic_idx: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➡️ Далее", callback_data="topic_test:next")],
            [InlineKeyboardButton(text="← К теме", callback_data=f"topic_test:back:{section_idx}:{subtopic_idx}")],
            [InlineKeyboardButton(text=MENU_BTN_HOME, callback_data="menu:back_to_main")],
        ]
    )


def topic_test_result_kb(section_idx: str, subtopic_idx: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Пройти ещё раз", callback_data="topic_test:retry")],
            [InlineKeyboardButton(text="← К теме", callback_data=f"topic_test:back:{section_idx}:{subtopic_idx}")],
            [InlineKeyboardButton(text=MENU_BTN_HOME, callback_data="menu:back_to_main")],
        ]
    )


def theory_page_kb(section_idx: str, subtopic_idx: str, page: int, total: int) -> InlineKeyboardMarkup:
    nav_row: List[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="← Назад", callback_data=f"topics:theory_page:{section_idx}:{subtopic_idx}:{page - 1}"))
    nav_row.append(InlineKeyboardButton(text=f"{page + 1} / {total}", callback_data="noop"))
    if page < total - 1:
        nav_row.append(InlineKeyboardButton(text="→ Далее", callback_data=f"topics:theory_page:{section_idx}:{subtopic_idx}:{page + 1}"))
    return InlineKeyboardMarkup(
        inline_keyboard=[
            nav_row,
            [InlineKeyboardButton(text="← К теме", callback_data=f"topics:subtopic:{section_idx}:{subtopic_idx}")],
            [InlineKeyboardButton(text=MENU_BTN_HOME, callback_data="menu:back_to_main")],
        ]
    )


def topic_card_front_kb(card_id: str, section_idx: str, subtopic_idx: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👁️ Показать ответ", callback_data=f"topic_card:reveal:{card_id}")],
            [InlineKeyboardButton(text="Закончить", callback_data="topic_card:finish")],
            [InlineKeyboardButton(text="← К теме", callback_data=f"topic_card:back:{section_idx}:{subtopic_idx}")],
        ]
    )


def topic_card_back_kb(card_id: str, section_idx: str, subtopic_idx: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Знал", callback_data=f"topic_card:mark:know:{card_id}"),
                InlineKeyboardButton(text="❌ Не знал", callback_data=f"topic_card:mark:unknown:{card_id}"),
            ],
            [InlineKeyboardButton(text="Закончить", callback_data="topic_card:finish")],
            [InlineKeyboardButton(text="← К теме", callback_data=f"topic_card:back:{section_idx}:{subtopic_idx}")],
        ]
    )


def topic_card_result_kb(section_idx: str, subtopic_idx: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Пройти ещё раз", callback_data="topic_card:retry")],
            [InlineKeyboardButton(text="← К теме", callback_data=f"topic_card:back:{section_idx}:{subtopic_idx}")],
            [InlineKeyboardButton(text=MENU_BTN_HOME, callback_data="menu:back_to_main")],
        ]
    )


def topic_ask_prompt_kb(section_idx: str, subtopic_idx: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="← К теме", callback_data=f"topic_ask:back:{section_idx}:{subtopic_idx}")],
            [InlineKeyboardButton(text=MENU_BTN_HOME, callback_data="menu:back_to_main")],
        ]
    )


def topic_ask_answer_kb(section_idx: str, subtopic_idx: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="← К теме", callback_data=f"topic_ask:back:{section_idx}:{subtopic_idx}")],
            [InlineKeyboardButton(text=MENU_BTN_HOME, callback_data="menu:back_to_main")],
        ]
    )


def mistakes_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="▶️ Начать повтор", callback_data="mistakes:repeat")],
            [InlineKeyboardButton(text=MENU_BTN_HOME, callback_data="menu:back_to_main")],
        ]
    )


def mistakes_question_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="← К ошибкам", callback_data="mistakes:start")],
            [InlineKeyboardButton(text=MENU_BTN_HOME, callback_data="menu:back_to_main")],
        ]
    )


def mistakes_result_kb(has_remaining: bool) -> InlineKeyboardMarkup:
    rows = []
    if has_remaining:
        rows.append([InlineKeyboardButton(text="🔄 Повторить оставшиеся", callback_data="mistakes:repeat")])
    rows.append([InlineKeyboardButton(text="← К ошибкам", callback_data="mistakes:start")])
    rows.append([InlineKeyboardButton(text=MENU_BTN_HOME, callback_data="menu:back_to_main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def flashcards_finish_kb(repeat_weak_available: bool) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    if repeat_weak_available:
        rows.append([InlineKeyboardButton(text="🔄 Повторить слабые", callback_data="flash:repeat_weak")])
    rows.append([InlineKeyboardButton(text=MENU_BTN_HOME, callback_data="menu:back_to_main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

