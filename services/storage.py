"""Пути к контенту — уровень 2 архитектуры AIda.

Два слоя, одинаковый формат файлов в обоих:

    data/subjects/{subject}/            базовый слой, общий для всех
        sheets_cache.json               снимок общих тестов и карточек
        theory_cache.json               теория по темам
        index/                          зарезервировано под общий FAISS

    data/teachers/{tg_id}/{subject}/    контент преподавателя
        uploads/                        оригиналы .docx (без картинок)
        tests.json                      результат разбора
        manifest.json                   что, когда, сколько, версия парсера
        index/                          зарезервировано

Код читает контент по пути и не знает, чей он. Какой путь дать — решает
политика источника из subjects.py.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional


logger = logging.getLogger(__name__)

DATA_ROOT = Path("data")
SUBJECTS_ROOT = DATA_ROOT / "subjects"
TEACHERS_ROOT = DATA_ROOT / "teachers"

# Имена файлов — одинаковые в обоих слоях
SHEETS_SNAPSHOT = "sheets_cache.json"
THEORY_SNAPSHOT = "theory_cache.json"
TESTS_FILE = "tests.json"
FLASHCARDS_FILE = "flashcards.json"
MANIFEST_FILE = "manifest.json"
UPLOADS_DIR = "uploads"
INDEX_DIR = "index"


# ---------- Базовый слой ----------

def base_dir(subject: str) -> Path:
    return SUBJECTS_ROOT / subject


def base_sheets_path(subject: str) -> Path:
    return base_dir(subject) / SHEETS_SNAPSHOT


def base_theory_path(subject: str) -> Path:
    return base_dir(subject) / THEORY_SNAPSHOT


def base_index_dir(subject: str) -> Path:
    return base_dir(subject) / INDEX_DIR


def ensure_base_dirs(subject: str) -> Path:
    d = base_dir(subject)
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------- Слой преподавателя ----------

def teacher_root(tg_id: int) -> Path:
    return TEACHERS_ROOT / str(tg_id)


def teacher_dir(tg_id: int, subject: str) -> Path:
    return teacher_root(tg_id) / subject


def teacher_uploads_dir(tg_id: int, subject: str) -> Path:
    return teacher_dir(tg_id, subject) / UPLOADS_DIR


def teacher_tests_path(tg_id: int, subject: str) -> Path:
    return teacher_dir(tg_id, subject) / TESTS_FILE


def teacher_flashcards_path(tg_id: int, subject: str) -> Path:
    return teacher_dir(tg_id, subject) / FLASHCARDS_FILE


def teacher_manifest_path(tg_id: int, subject: str) -> Path:
    return teacher_dir(tg_id, subject) / MANIFEST_FILE


def teacher_index_dir(tg_id: int, subject: str) -> Path:
    return teacher_dir(tg_id, subject) / INDEX_DIR


def ensure_teacher_dirs(tg_id: int, subject: str) -> Path:
    """Создаёт папки преподавателя под предмет. Вызывается при регистрации."""
    d = teacher_dir(tg_id, subject)
    d.mkdir(parents=True, exist_ok=True)
    teacher_uploads_dir(tg_id, subject).mkdir(parents=True, exist_ok=True)
    return d


def teacher_has_content(tg_id: int, subject: str) -> bool:
    return teacher_tests_path(tg_id, subject).exists()


# ---------- Чтение и запись JSON ----------

def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        logger.exception("Не удалось прочитать %s", path)
        return default


def write_json(path: Path, data: Any) -> bool:
    """Пишет атомарно через временный файл — чтобы не оставить битый JSON."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
        return True
    except Exception:  # noqa: BLE001
        logger.exception("Не удалось записать %s", path)
        return False


def dir_size_mb(path: Path) -> float:
    """Размер папки в мегабайтах — для контроля места на сервере."""
    if not path.exists():
        return 0.0
    total = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    return round(total / 1_000_000, 2)


def find_legacy_path(*candidates: Path) -> Optional[Path]:
    """Первый существующий путь из списка — для переезда со старой раскладки."""
    for c in candidates:
        if c.exists():
            return c
    return None
