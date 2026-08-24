"""Разделы преподавателя: список, названия и подсказка по имени файла.

Раздел привязан к файлу целиком, а не к отдельному вопросу. Так сделано
потому, что `tests.json` всегда пересобирается из папки `uploads/`: если
писать раздел в строки вопросов, первая же пересборка его сотрёт. Привязка
живёт в `manifest.json` рядом с именем файла и переживает смену версии
парсера.

Сетка разделов берётся из `subjects.py`. Своих преподаватель может добавить
сколько угодно — они получают ключи вида `c1`, `c2` и лежат в том же
манифесте.

Логика здесь чистая: ни бота, ни диска. Всё, что связано с файлами, —
в `services/teacher_content.py`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional

import subjects as subjects_cfg


# Ключ раздела у файла, для которого преподаватель раздел не выбрал.
# Пустая строка, а не None: ровно это значение попадает в поле «Раздел».
NONE_KEY = ""

# Как этот случай называется на экранах. Не «без раздела»: файл, где вопросы
# разных разделов вперемешку, — законное место назначения, а не отложенное
# решение. Название говорит о составе, а не о происхождении файла, и одно
# и то же у преподавателя при загрузке и у ученика в списке разделов.
UNSORTED_TITLE = "🔀 Смешанные вопросы"

CUSTOM_PREFIX = "c"

# Длина названия своего раздела: больше не влезает в кнопку
MAX_TITLE = 40

# Разделитель уровней в ключе: "6" — раздел, "6_1" — тема внутри него.
# Тот же символ, что в кодах тем базового контента, поэтому ключ темы
# у преподавателя и у общих вопросов читается одинаково.
TOPIC_SEP = "_"

# Сколько символов названия темы влезает в кнопку, не превращая её в простыню
MAX_BUTTON = 38


@dataclass(frozen=True)
class Section:
    key: str
    title: str
    full: str = ""

    @property
    def label(self) -> str:
        """Подпись для кнопки: номер и короткое название."""
        return f"{self.key}. {self.title}" if self.key.isdigit() else self.title


def base_sections(subject: str) -> List[Section]:
    return [
        Section(key=s["key"], title=s["title"], full=s.get("full", ""))
        for s in subjects_cfg.subject_sections(subject)
    ]


def merged(subject: str, custom: Optional[Dict[str, str]] = None) -> List[Section]:
    """Разделы программы плюс свои, добавленные преподавателем."""
    out = base_sections(subject)
    for key, title in sorted((custom or {}).items()):
        out.append(Section(key=key, title=title))
    return out


def title_of(subject: str, key: str, custom: Optional[Dict[str, str]] = None) -> str:
    """Название раздела по ключу. Неизвестный ключ не прячем — показываем как есть."""
    if not key:
        return UNSORTED_TITLE
    if is_topic(key):
        return topic_title(subject, key)
    for section in merged(subject, custom):
        if section.key == key:
            return section.label
    return f"Раздел {key}"


def is_valid(
    subject: str,
    key: str,
    custom: Optional[Dict[str, str]] = None,
    custom_topics: Optional[Dict[str, str]] = None,
) -> bool:
    """Существует ли такой раздел или такая тема.

    Тему проверяем внутри её раздела: ключ несёт раздел в себе, поэтому
    «6_1» в разделе 7 не подтвердится, даже если такая тема есть у соседа.
    """
    if key == NONE_KEY:
        return True
    if is_topic(key):
        section = section_of(key)
        if not is_valid(subject, section, custom):
            return False
        return any(t.key == key for t in merged_topics(subject, section, custom_topics))
    return any(s.key == key for s in merged(subject, custom))


def next_custom_key(custom: Optional[Dict[str, str]] = None) -> str:
    """Следующий свободный ключ своего раздела.

    Считаем от максимума, а не от количества: иначе после удаления раздела
    ключ переиспользуется и старые файлы уедут в чужой раздел.
    """
    used = []
    for key in (custom or {}):
        if key.startswith(CUSTOM_PREFIX) and key[len(CUSTOM_PREFIX):].isdigit():
            used.append(int(key[len(CUSTOM_PREFIX):]))
    return f"{CUSTOM_PREFIX}{max(used, default=0) + 1}"


def clean_title(text: str) -> str:
    """Название своего раздела из того, что ввёл преподаватель."""
    title = " ".join((text or "").split())
    return title[:MAX_TITLE]


# ---------- Темы: второй уровень внутри раздела ----------
#
# Ключ темы начинается с ключа раздела: "6_1" лежит в разделе "6", своя тема
# преподавателя в том же разделе — "6_c1". Из этого следует главное свойство:
# **раздел всегда восстанавливается из темы**, поэтому в поле «Раздел» строки
# вопроса хранится один ключ — либо раздела, либо темы, — и старые материалы,
# разложенные до появления тем, продолжают работать без пересборки.


def section_of(key: str) -> str:
    """Раздел, которому принадлежит ключ. Для ключа раздела — он сам."""
    return (key or "").split(TOPIC_SEP)[0]


def is_topic(key: str) -> bool:
    return TOPIC_SEP in (key or "")


def base_topics(subject: str, section: str) -> List[Section]:
    """Темы программы внутри раздела. Пусто — если сетки тем нет."""
    for item in subjects_cfg.subject_sections(subject):
        if item["key"] == section:
            return [
                Section(key=t["key"], title=t["title"])
                for t in (item.get("topics") or [])
            ]
    return []


def merged_topics(
    subject: str,
    section: str,
    custom: Optional[Dict[str, str]] = None,
) -> List[Section]:
    """Темы программы плюс свои, добавленные преподавателем в этот раздел.

    `custom` — все свои темы преподавателя по всем разделам сразу: ключ несёт
    в себе раздел, поэтому отбор идёт по нему, а не по отдельному полю.
    """
    out = base_topics(subject, section)
    own = {
        key: title
        for key, title in (custom or {}).items()
        if section_of(key) == section
    }
    for key, title in sorted(own.items()):
        out.append(Section(key=key, title=title))
    return out


def topic_title(
    subject: str,
    key: str,
    custom: Optional[Dict[str, str]] = None,
) -> str:
    """Название темы по ключу. Неизвестный ключ показываем как есть."""
    for topic in merged_topics(subject, section_of(key), custom):
        if topic.key == key:
            return topic.title
    return key


def next_custom_topic_key(
    section: str,
    custom: Optional[Dict[str, str]] = None,
) -> str:
    """Следующий свободный ключ своей темы в этом разделе: "6_c1", "6_c2"…

    Как и у разделов, счёт идёт от максимума, а не от количества: иначе
    после удаления темы ключ переиспользуется и старые файлы уедут в чужую.
    """
    prefix = f"{section}{TOPIC_SEP}{CUSTOM_PREFIX}"
    used = []
    for key in (custom or {}):
        if key.startswith(prefix) and key[len(prefix):].isdigit():
            used.append(int(key[len(prefix):]))
    return f"{prefix}{max(used, default=0) + 1}"


def button_label(title: str) -> str:
    """Подпись темы для кнопки: длинные названия режем по границе фразы.

    Названия тем в программе — это перечисления через точку («Реформация.
    Религиозные войны… Контрреформация»). Первая фраза узнаётся, а целиком
    такая строка превращает клавиатуру в стену текста.
    """
    title = " ".join((title or "").split())
    if len(title) <= MAX_BUTTON:
        return title

    head = title.split(". ")[0]
    if len(head) <= MAX_BUTTON:
        return head + "…"
    return title[: MAX_BUTTON - 1].rstrip(" ,.—-") + "…"


def place_title(
    subject: str,
    key: str,
    custom_sections: Optional[Dict[str, str]] = None,
    custom_topics: Optional[Dict[str, str]] = None,
) -> str:
    """Куда положен файл, одной строкой: раздел или «раздел → тема».

    Единственное место, где ключ превращается в человеческую подпись.
    Экраны загрузки, состава тренажёра и списка файлов зовут её, а не
    собирают строку сами — иначе одно и то же место называлось бы
    по-разному в трёх местах.
    """
    if not key:
        return UNSORTED_TITLE
    section = section_of(key)
    section_name = title_of(subject, section, custom_sections)
    if not is_topic(key):
        return section_name
    return f"{section_name} → {topic_title(subject, key, custom_topics)}"


# ---------- Подсказка по имени файла ----------

# Порядок важен: длинные записи должны проверяться раньше коротких,
# иначе «XIX» разберётся как «XI» + «X».
_ROMAN = [
    ("xxi", 21), ("xviii", 18), ("xvii", 17), ("xvi", 16),
    ("xix", 19), ("xx", 20), ("xv", 15), ("xiv", 14),
]

# Кириллические буквы, неотличимые от латинских на глаз. В именах файлов
# «ХІХ век» сплошь и рядом набрано кириллицей.
_HOMOGLYPHS = str.maketrans({"х": "x", "і": "i", "ѵ": "v", "с": "c"})

_YEAR_RE = re.compile(r"\b(1[0-9]{3}|20[0-9]{2})\b")

# Сколько даёт совпадение: слово весит больше, чем попадание года,
# потому что год легко залетает в соседний раздел на границе периодов.
_HINT_WEIGHT = 3
_YEAR_WEIGHT = 2


def _normalize(filename: str) -> str:
    """Имя файла без расширения и разделителей, в нижнем регистре."""
    name = re.sub(r"\.docx$", "", filename or "", flags=re.IGNORECASE)
    name = name.lower()
    return re.sub(r"[_\-–—.,()\[\]]+", " ", name)


def _years_in(text: str) -> List[int]:
    return [int(y) for y in _YEAR_RE.findall(text)]


def _centuries_in(text: str) -> List[tuple]:
    """Диапазоны лет для веков, записанных римскими цифрами."""
    romanized = text.translate(_HOMOGLYPHS)
    found: List[tuple] = []
    for token in romanized.split():
        for roman, century in _ROMAN:
            if token == roman:
                found.append(((century - 1) * 100 + 1, century * 100))
                break
    return found


def _score(section: dict, name: str, years: List[int], centuries: List[tuple]) -> int:
    score = 0
    for hint in section.get("hints") or []:
        if hint in name:
            score += _HINT_WEIGHT

    span = section.get("years")
    if span:
        start, end = span
        score += _YEAR_WEIGHT * sum(1 for y in years if start <= y <= end)
        score += _YEAR_WEIGHT * sum(1 for a, b in centuries if a <= end and b >= start)

    return score


def suggest(filename: str, subject: str) -> Optional[str]:
    """Ключ раздела, на который похоже имя файла. None — если непонятно.

    Молчать лучше, чем подсказывать наугад: неверная подсказка, которую
    подтвердили не глядя, тише ломает материалы, чем лишний вопрос.
    """
    sections = subjects_cfg.subject_sections(subject)
    if not sections:
        return None

    name = _normalize(filename)
    years = _years_in(name)
    centuries = _centuries_in(name)

    scored = sorted(
        ((_score(s, name, years, centuries), s["key"]) for s in sections),
        key=lambda pair: (-pair[0], pair[1]),
    )
    best_score, best_key = scored[0]
    second = scored[1][0] if len(scored) > 1 else 0

    # Побеждать нужно уверенно: при равном счёте это монетка, а не подсказка
    if best_score < _YEAR_WEIGHT or best_score == second:
        return None
    return best_key
