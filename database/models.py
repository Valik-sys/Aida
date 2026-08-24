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
