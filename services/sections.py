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
    for section in merged(subject, custom):
        if section.key == key:
            return section.label
    return f"Раздел {key}"


def is_valid(subject: str, key: str, custom: Optional[Dict[str, str]] = None) -> bool:
    if key == NONE_KEY:
        return True
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
