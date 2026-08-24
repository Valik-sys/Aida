"""Кто какой контент видит.

Единственное место, где решается вопрос «откуда брать вопросы». Хендлеры
запрашивают набор по пользователю и не знают, чей это контент — общий предмета
или конкретного преподавателя. Политику задаёт subjects.SUBJECTS[...]["sources"],
так что включить смешивание общего с учительским можно правкой конфига,
а не переписыванием режимов.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import subjects as subjects_cfg
from database.db import (
    get_hidden_questions,
    get_student_reported,
    get_teacher_setting,
    get_user,
    question_hash,
)
from database.models import ROLE_TEACHER, User
from services import reports, teacher_content
from services.sheets import sheets_cache


logger = logging.getLogger(__name__)


# Причины, по которым набор оказался пустым. Текст уходит пользователю,
# поэтому формулировки разные для ученика и преподавателя.
NO_TEACHER = (
    "Ты пока не привязан к преподавателю.\n"
    "Попроси у него код приглашения или ссылку."
)
TEACHER_HAS_NO_CONTENT = (
    "Твой преподаватель ещё не загрузил задания.\n"
    "Как только загрузит — тренажёр появится здесь."
)
OWN_CONTENT_EMPTY = (
    "Вы ещё не загрузили билеты.\n"
    "Нажмите «📥 Загрузить билеты» и пришлите файл .docx."
)
BASE_EMPTY = "Материалы пока недоступны. Попробуйте позже."


@dataclass
class ContentBundle:
    """Набор вопросов и всё, что нужно знать о его происхождении."""

    rows: List[Dict[str, str]] = field(default_factory=list)
    source: str = subjects_cfg.SOURCE_BASE
    subject: str = subjects_cfg.DEFAULT_SUBJECT
    teacher_id: Optional[int] = None
    empty_reason: Optional[str] = None
    # True, если своих вопросов у преподавателя нет и подставлен общий набор
    fallback: bool = False

    def __bool__(self) -> bool:
        return bool(self.rows)


def resolve_teacher_id(user: User) -> Optional[int]:
    """Чей контент показывать пользователю.

    Преподавателю — его собственный: так работает режим «пройти свой тренажёр»,
    и отдельная ветка кода для превью не нужна.
    """
    if user.role == ROLE_TEACHER:
        return user.telegram_id
    return user.teacher_id


# Ключ настройки «откуда брать материалы». Одна на весь тренажёр.
MATERIALS_SOURCE = "materials_source"


async def effective_source(user: User, subject: str) -> str:
    """Источник материалов с учётом выбора преподавателя.

    Конфиг предмета задаёт значение по умолчанию, `allowed_sources` — что
    вообще доступно (сюда позже добавится тариф), а преподаватель выбирает
    внутри доступного. Ученик наследует выбор своего преподавателя.
    """
    allowed = subjects_cfg.allowed_sources(subject)

    # Значение из конфига тоже приводим к доступному: у предмета без общей базы
    # умолчание «base» недостижимо и увело бы на чужой набор вопросов
    default = subjects_cfg.mode_source(subject, "tests")
    if default not in allowed:
        default = allowed[0]

    teacher_id = resolve_teacher_id(user)
    if not teacher_id:
        return default

    chosen = await get_teacher_setting(teacher_id, subject, MATERIALS_SOURCE)
    if chosen and chosen in allowed:
        return chosen
    return default


async def _drop_reported(
    rows: List[Dict[str, str]], user: User, teacher_id: Optional[int]
) -> List[Dict[str, str]]:
    """Убирает вопросы, которых этому человеку видеть не нужно.

    Два случая: вопрос скрыт у всех — преподаватель убрал его или набралось
    достаточно жалоб на брак; и вопрос, на который пожаловался лично этот
    ученик. Непонятые вопросы остаются в обороте: их и надо прорешать.
    """
    if not rows or not teacher_id:
        return rows

    hidden = await get_hidden_questions(teacher_id, list(reports.INVISIBLE))
    mine = await get_student_reported(user.telegram_id, list(reports.BROKEN_REASONS))
    skip = hidden | mine
    if not skip:
        return rows

    kept = [r for r in rows if question_hash(r.get("Вопрос") or "") not in skip]
    if len(kept) != len(rows):
        logger.info(
            "Скрыто вопросов для %s: %d (всего %d)",
            user.telegram_id, len(rows) - len(kept), len(rows),
        )
    return kept


async def get_tests(telegram_id: int) -> ContentBundle:
    """Вопросы для теста по политике предмета."""
    user = await get_user(telegram_id)
    if not user:
        return ContentBundle(empty_reason=BASE_EMPTY)

    subject = user.current_subject or subjects_cfg.DEFAULT_SUBJECT
    source = await effective_source(user, subject)
    allow_base = source == subjects_cfg.SOURCE_TEACHER_THEN_BASE

    # «Только общие» — учительский слой не смотрим вовсе
    if source == subjects_cfg.SOURCE_BASE:
        rows = list(sheets_cache.tests_rows or [])
        teacher_id = resolve_teacher_id(user)
        # Убранное преподавателем прячется и в общем наборе: скрытие
        # хранится по нему, чужих учеников оно не касается
        rows = await _drop_reported(rows, user, teacher_id)
        return ContentBundle(
            rows=rows,
            source=subjects_cfg.SOURCE_BASE,
            subject=subject,
            teacher_id=teacher_id,
            empty_reason=None if rows else BASE_EMPTY,
        )

    if source in (subjects_cfg.SOURCE_TEACHER, subjects_cfg.SOURCE_TEACHER_THEN_BASE):
        teacher_id = resolve_teacher_id(user)

        # Без преподавателя общий набор не подставляем: иначе ученик, который
        # обошёл ввод кода, получает рабочий тренажёр и никогда не привяжется
        if not teacher_id:
            return ContentBundle(source=source, subject=subject, empty_reason=NO_TEACHER)

        rows = teacher_content.load_tests(teacher_id, subject) if teacher_id else []
        if rows:
            rows = await _drop_reported(rows, user, teacher_id)
            return ContentBundle(
                rows=rows,
                source=subjects_cfg.SOURCE_TEACHER,
                subject=subject,
                teacher_id=teacher_id,
            )

        # Своих вопросов нет. При политике «а пока — общие» отдаём базовый
        # набор предмета: тренажёр работает сразу, а не после первой загрузки.
        if allow_base:
            base_rows = list(sheets_cache.tests_rows or [])
            if base_rows:
                base_rows = await _drop_reported(base_rows, user, teacher_id)
                return ContentBundle(
                    rows=base_rows,
                    source=subjects_cfg.SOURCE_BASE,
                    subject=subject,
                    teacher_id=teacher_id,
                    fallback=True,
                )

        reason = (
            OWN_CONTENT_EMPTY if user.role == ROLE_TEACHER else TEACHER_HAS_NO_CONTENT
        )
        return ContentBundle(
            source=source, subject=subject, teacher_id=teacher_id, empty_reason=reason
        )

    rows = list(sheets_cache.tests_rows or [])
    return ContentBundle(
        rows=rows,
        source=subjects_cfg.SOURCE_BASE,
        subject=subject,
        empty_reason=None if rows else BASE_EMPTY,
    )


FLASHCARD_CATEGORIES = ("dates", "concepts", "persons")


def _base_flashcards() -> Dict[str, List[Dict[str, Any]]]:
    return {
        "dates": sheets_cache.flash_dates or [],
        "concepts": sheets_cache.flash_concepts or [],
        "persons": sheets_cache.flash_persons or [],
    }


def get_flashcards(
    subject: Optional[str] = None,
    teacher_id: Optional[int] = None,
) -> Dict[str, List[Dict[str, Any]]]:
    """Карточки по политике предмета.

    Функция синхронная: базовый слой уже лежит в памяти, ходить в БД незачем.
    Сейчас у всех предметов политика «base» — карточки общие для всех.
    Ветка преподавателя написана заранее, чтобы включение его карточек
    свелось к правке политики в subjects.py и появлению файла на диске.
    """
    subject = subject or subjects_cfg.DEFAULT_SUBJECT
    source = subjects_cfg.mode_source(subject, "flashcards")

    if source == subjects_cfg.SOURCE_TEACHER and teacher_id:
        own = teacher_content.load_flashcards(teacher_id, subject)
        # Файл может существовать, но быть пустым — тогда это не «свои карточки»
        if any(own.values()):
            return own
        logger.info(
            "У преподавателя %s нет своих карточек по %s — отдаём общие",
            teacher_id, subject,
        )

    return _base_flashcards()


def get_flashcards_category(
    category: str,
    subject: Optional[str] = None,
    teacher_id: Optional[int] = None,
) -> List[Dict[str, Any]]:
    return get_flashcards(subject, teacher_id).get(category, [])


def variants_of(rows: List[Dict[str, str]]) -> List[str]:
    """Список вариантов в наборе — для меню выбора."""
    return sorted({
        (r.get("Вариант") or "").strip()
        for r in rows
        if (r.get("Вариант") or "").strip()
    })
