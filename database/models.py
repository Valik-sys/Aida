from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


ROLE_TEACHER = "teacher"
ROLE_STUDENT = "student"


@dataclass(slots=True)
class User:
    telegram_id: int
    role: str | None = None
    # У ученика — telegram_id его преподавателя. У преподавателя — None.
    teacher_id: int | None = None
    current_subject: str | None = None
    name: str = ""
    # Класс спрашиваем только у ученика.
    class_name: str = ""
    created_at: datetime | None = None
    last_active_at: datetime | None = None
    # Из телеграма, а не из анкеты: @username и имя в профиле. Нужны, чтобы
    # написать человеку по заявке и чтобы в списках он был не «ID 481234567».
    # Имя анкеты (`name`) ими не подменяется — его ученик вводил сам.
    username: str = ""
    tg_name: str = ""

    @property
    def is_teacher(self) -> bool:
        return self.role == ROLE_TEACHER

    @property
    def is_student(self) -> bool:
        return self.role == ROLE_STUDENT


@dataclass(slots=True)
class Teacher:
    tg_user_id: int
    subject: str
    invite_code: str
    created_at: datetime | None = None


@dataclass(slots=True)
class Subscription:
    """Доступ преподавателя. На человека, а не на предмет.

    Расходы зависят от числа учеников, а не от числа предметов: ученик
    по русскому стоит столько же, сколько по истории. Поэтому и лимит,
    и срок — общие на человека.
    """

    tg_user_id: int
    # Номер телефона в каноническом виде «+375291234567» — общий ключ
    # с внешней кассой. Пустой, пока преподаватель им не поделился.
    phone: str = ""
    trial_until: datetime | None = None
    paid_until: datetime | None = None
    student_limit: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None
