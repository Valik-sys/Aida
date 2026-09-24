"""Жалобы учеников на вопросы: причины и правила скрытия.

Кнопка «Проблема с вопросом» ловит две разные вещи, и путать их нельзя:

  • **брак разбора** — потерялись варианты, вопрос собран криво. Это наша
    поломка, вопрос надо убирать из оборота.
  • **не понял формулировку** — вопрос целый, ученик о него споткнулся.
    Это сигнал преподавателю объяснить, а не признак поломки.

Отсюда главное правило: к порогу автоскрытия считаются только жалобы на
брак. Если бы «не понял» тоже скрывал, из тренажёра исчезали бы ровно
самые сложные вопросы — те, что и надо разбирать, потому что на экзамене
формулировка будет та же.

Логика здесь чистая: ни бота, ни базы.
"""

from __future__ import annotations

from typing import Dict, Iterable

# Причины, между которыми выбирает ученик
REASON_UNCLEAR = "unclear"
REASON_NO_OPTIONS = "no_options"
REASON_INCORRECT = "incorrect"

# Порядок такой же, как на экране выбора
REASON_LABELS: Dict[str, str] = {
    REASON_UNCLEAR: "Не понял, о чём вопрос",
    REASON_NO_OPTIONS: "Нет вариантов ответа",
    REASON_INCORRECT: "Вопрос составлен некорректно",
}

# Как причина читается в списке у преподавателя
REASON_SHORT: Dict[str, str] = {
    REASON_UNCLEAR: "не понял вопрос",
    REASON_NO_OPTIONS: "нет вариантов ответа",
    REASON_INCORRECT: "составлен некорректно",
}

# Жалобы, которые заявляют поломку, а не сложность
BROKEN_REASONS = frozenset({REASON_NO_OPTIONS, REASON_INCORRECT})

# Сколько жалоб на брак прячут вопрос у всех. Двойка, а не тройка:
# у репетитора обычно 2–10 учеников, и порог 3 для маленькой группы
# недостижим — вопрос висел бы сломанным до конца года.
HIDE_THRESHOLD = 2

# Общий вопрос базы — не преподавательский, и правило для него другое.
# Он скрывается у всех сразу, но только когда на брак пожаловались ученики
# хотя бы двух РАЗНЫХ преподавателей. Иначе двое друзей из одной группы
# вычищали бы общую базу для всех остальных. Решение о возврате принимает
# админ, а не преподаватель: вопрос писал не он, и война флагов
# «один вернул — другой скрыл» не нужна (решено 13.09.2026).
BASE_HIDE_TEACHERS = 2

# Статусы вопроса
STATUS_HIDDEN = "hidden"        # скрыт автоматически по жалобам
STATUS_REMOVED = "removed"      # убран преподавателем
STATUS_CONFIRMED = "confirmed"  # преподаватель подтвердил, что вопрос исправен

# При этих статусах вопрос ученикам не показывается
INVISIBLE = frozenset({STATUS_HIDDEN, STATUS_REMOVED})


def is_valid_reason(reason: str) -> bool:
    return reason in REASON_LABELS


def is_broken(reason: str) -> bool:
    """Жалоба заявляет поломку вопроса, а не сложность."""
    return reason in BROKEN_REASONS


def count_broken(reasons: Iterable[str]) -> int:
    return sum(1 for r in reasons if is_broken(r))


def should_hide(reasons: Iterable[str], status: str | None = None) -> bool:
    """Пора ли прятать вопрос у всех.

    Подтверждённый преподавателем вопрос не прячется больше никогда:
    иначе следующие двое учеников убрали бы его снова, и так по кругу.
    """
    if status in (STATUS_CONFIRMED, STATUS_REMOVED, STATUS_HIDDEN):
        return False
    return count_broken(reasons) >= HIDE_THRESHOLD


def should_hide_base(broken_teachers: int, status: str | None = None) -> bool:
    """Пора ли прятать общий вопрос у всех.

    Считаются не жалобы, а преподаватели, чьи ученики пожаловались на брак.
    Возвращённый админом вопрос больше не прячется — по той же причине,
    что и у преподавателя: иначе его убирали бы снова и снова.
    """
    if status in (STATUS_CONFIRMED, STATUS_REMOVED, STATUS_HIDDEN):
        return False
    return broken_teachers >= BASE_HIDE_TEACHERS


def hides_for_student(reason: str) -> bool:
    """Прятать ли вопрос у того, кто пожаловался.

    Сломанный — да, показывать его снова незачем. Непонятый — нет:
    именно его и нужно прорешать после объяснения преподавателя.
    """
    return is_broken(reason)
