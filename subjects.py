"""Конфигурация предметов — уровень 1 архитектуры AIda.

Предмет задаёт «как работает»: какие режимы включены и откуда берётся контент.
Преподаватель задаёт «на чём работает» — это уровень 2, см. services/storage.py.

Добавление нового предмета = запись в SUBJECTS + папка с базовым контентом
в data/subjects/{ключ}/. Код при этом не меняется.
"""

from __future__ import annotations

from typing import Dict, List


# Режимы бота. Ключ → как называется кнопка в меню.
# Здесь только справочник; какие из них включены — решает предмет.
MODE_LABELS: Dict[str, str] = {
    "tests": "🎯 Тренировка",
    "flashcards": "⚡ Карточки",
    "ask": "❓ Задать вопрос",
    "topics": "🗂 По темам",
    "mistakes": "🔁 Мои ошибки",
    "progress": "📈 Мой прогресс",
}

# Источник контента для режима:
#   "base"    — общий контент предмета из data/subjects/{subject}/
#   "teacher" — контент преподавателя из data/teachers/{tg_id}/{subject}/
# Позже появится "base+teacher" — слияние; код провайдера к этому готовится,
# но пока такой политики ни у кого нет.
SOURCE_BASE = "base"
SOURCE_TEACHER = "teacher"
# Контент преподавателя, а пока его нет — общий по предмету. Так тренажёр
# полезен с первого дня, ещё до того как преподаватель что-то загрузил.
SOURCE_TEACHER_THEN_BASE = "teacher_then_base"


# Разделы предмета — крупная сетка программы, в которую преподаватель
# раскладывает свои файлы. Ключи те же, что в поле «Раздел» базового
# контента, поэтому его вопросы и общие лежат в одной системе координат.
#
#   key   — попадает в строку вопроса
#   title — короткая подпись для кнопки
#   full  — полное название для экранов
#   hints — слова, по которым угадывается раздел из имени файла
#   years — годы раздела; из имени файла вытаскиваются годы и века
#
# Темы (второй уровень) появятся внутри разделов позже: формат тот же,
# добавится ключ вида "6.2" — экраны от этого не изменятся.
_HISTORY_SECTIONS: List[dict] = [
    {
        "key": "1",
        "title": "Древнейшие цивилизации",
        "full": "1. Становление древнейших цивилизаций. Беларусь в догосударственный период",
        "hints": ["первобыт", "древнейш", "древност", "цивилизац", "каменный век",
                  "происхождение человека", "древний мир", "античн", "верования"],
        "years": None,
    },
    {
        "key": "2",
        "title": "Раннее средневековье",
        "full": "2. Мир в эпоху Раннего и Высокого средневековья. Первые государства на территории Беларуси",
        "hints": ["средневеков", "средних веков", "полоцк", "туровск", "княжеств",
                  "крещение", "srednevekov", "knyazhestv"],
        "years": (500, 1300),
    },
    {
        "key": "3",
        "title": "ВКЛ, Позднее средневековье",
        "full": "3. Позднее средневековье. Беларусь в период ВКЛ",
        "hints": ["вкл", "великое княжество литовское", "литовск", "грюнвальд",
                  "статут", "позднем средневековье", "позднее средневековье"],
        "years": (1300, 1600),
    },
    {
        "key": "4",
        "title": "Раннее Новое время",
        "full": "4. Беларусь и мир в период раннего Нового времени",
        "hints": ["новое время", "нового времени", "речь посполит", "разделы речи",
                  "просвещени", "реформац", "возрождени", "гуманизм"],
        "years": (1600, 1800),
    },
    {
        "key": "5",
        "title": "XIX — начало XX в.",
        "full": "5. Беларусь и мир в XIX — начале XX в.",
        "hints": ["российской империи", "российская империя", "промышленная революция",
                  "промышленной революции", "сельского хозяйства", "индустриальному обществу",
                  "восстание", "народности к нации"],
        "years": (1795, 1917),
    },
    {
        "key": "6",
        "title": "1917–1945",
        "full": "6. Беларусь и мир в 1917–1945 гг.",
        "hints": ["бсср", "нэп", "военный коммунизм", "индустриализац", "коллективизац",
                  "западная беларусь", "вторая мировая", "великая отечественная",
                  "оккупац", "партизан", "октябрьской революц", "февральской"],
        "years": (1917, 1945),
    },
    {
        "key": "7",
        "title": "1945–1991",
        "full": "7. Беларусь и мир во второй половине 40-х — начале 90-х гг. XX в.",
        "hints": ["послевоенн", "оттепель", "застой", "перестройк", "холодная война"],
        "years": (1945, 1991),
    },
    {
        "key": "8",
        "title": "Конец XX — XXI в.",
        "full": "8. Республика Беларусь и мир в конце XX — первой четверти XXI в.",
        "hints": ["республика беларусь", "суверен", "независим", "современн"],
        "years": (1991, 2035),
    },
]


SUBJECTS: Dict[str, dict] = {
    "history": {
        "name": "История Беларуси",
        "emoji": "📜",
        # Разделы программы — сетка, в которую преподаватель кладёт файлы
        "sections": _HISTORY_SECTIONS,
        # Режимы, видимые в меню ученика
        "modes": ["tests", "flashcards", "ask", "mistakes", "progress"],
        # Режимы, которые есть в коде и работают, но спрятаны на время прототипа.
        # Вернуть в меню = перенести ключ в "modes".
        "hidden_modes": ["topics"],
        "sources": {
            "tests": SOURCE_TEACHER_THEN_BASE,
            "flashcards": SOURCE_BASE,
            "ask": SOURCE_BASE,
            "topics": SOURCE_BASE,
            "mistakes": SOURCE_BASE,
        },
        # Что преподаватель может загружать сам
        "teacher_uploads": ["tests"],
        # Есть общий набор вопросов и карточек по предмету
        "base_content": True,
    },
    "russian": {
        "name": "Русский язык",
        "emoji": "✍️",
        "modes": ["ask"],
        "hidden_modes": [],
        "sources": {
            "ask": SOURCE_BASE,
        },
        "teacher_uploads": [],
        # Предмет заведён архитектурно: в меню есть, контента пока нет.
        "stub": True,
    },
}

DEFAULT_SUBJECT = "history"


def get_subject(subject: str) -> dict:
    """Конфиг предмета. Неизвестный ключ → предмет по умолчанию."""
    return SUBJECTS.get(subject) or SUBJECTS[DEFAULT_SUBJECT]


def subject_name(subject: str) -> str:
    cfg = get_subject(subject)
    return cfg["name"]


def subject_title(subject: str) -> str:
    """Название с эмодзи — для кнопок выбора предмета."""
    cfg = get_subject(subject)
    emoji = cfg.get("emoji", "")
    return f"{emoji} {cfg['name']}".strip()


def visible_modes(subject: str) -> List[str]:
    return list(get_subject(subject).get("modes", []))


def is_mode_enabled(subject: str, mode: str) -> bool:
    """Виден ли режим в меню. Скрытые режимы сюда не попадают."""
    return mode in get_subject(subject).get("modes", [])


def is_mode_available(subject: str, mode: str) -> bool:
    """Существует ли режим у предмета вообще — включая скрытые.

    Нужно, чтобы отличить «спрятали на время» от «у этого предмета такого нет».
    """
    cfg = get_subject(subject)
    return mode in cfg.get("modes", []) or mode in cfg.get("hidden_modes", [])


def mode_source(subject: str, mode: str) -> str:
    """Откуда брать контент для режима. По умолчанию — базовый слой."""
    return get_subject(subject).get("sources", {}).get(mode, SOURCE_BASE)


def teacher_can_upload(subject: str, kind: str = "tests") -> bool:
    return kind in get_subject(subject).get("teacher_uploads", [])


def has_base_content(subject: str) -> bool:
    """Есть ли у предмета общий набор вопросов."""
    return bool(get_subject(subject).get("base_content"))


def allowed_sources(subject: str) -> List[str]:
    """Какие источники доступны преподавателю по этому предмету.

    Здесь же будет учитываться тариф, когда появится подписка: список
    сузится, а логика выбора и сама настройка не изменятся.
    """
    if not has_base_content(subject):
        return [SOURCE_TEACHER]
    return [SOURCE_TEACHER, SOURCE_TEACHER_THEN_BASE, SOURCE_BASE]


# Подписи для экрана настроек
SOURCE_LABELS: Dict[str, str] = {
    SOURCE_TEACHER: "Только мои материалы",
    SOURCE_TEACHER_THEN_BASE: "Мои, а пока их нет — общие",
    SOURCE_BASE: "Только общие материалы",
}

SOURCE_HINTS: Dict[str, str] = {
    SOURCE_TEACHER: "Ученики занимаются строго по вашим билетам.",
    SOURCE_TEACHER_THEN_BASE: "Пока вы не загрузили свои — работают общие вопросы курса.",
    SOURCE_BASE: "Общие вопросы курса, ваши материалы не используются.",
}


def subject_sections(subject: str) -> List[dict]:
    """Разделы программы предмета. Пустой список — предмет без сетки."""
    return list(get_subject(subject).get("sections") or [])


def has_sections(subject: str) -> bool:
    return bool(get_subject(subject).get("sections"))


def is_stub(subject: str) -> bool:
    """Предмет заведён, но контента под него ещё нет."""
    return bool(get_subject(subject).get("stub"))


def all_subject_keys() -> List[str]:
    return list(SUBJECTS.keys())
