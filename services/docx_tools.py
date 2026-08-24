"""Работа с .docx до разбора: вычистка картинок и базовые проверки.

Зачем: в учительских билетах картинки занимают почти весь объём файла
(в наших образцах 0,70 МБ из 0,75 МБ), а парсеру нужен только текст.
Убираем их при загрузке — тогда оригиналы можно хранить вечно и перегонять
новой версией парсера, не прося преподавателей перезаливать файлы.

Картинки не удаляются, а заменяются заглушкой 1×1: связи внутри документа
остаются целыми, файл открывается и разбирается как обычно.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import zipfile
from pathlib import Path
from typing import List, Tuple


logger = logging.getLogger(__name__)

MEDIA_PREFIX = "word/media/"

# Прозрачный PNG 1×1 — вместо него подставляется любая картинка
_PLACEHOLDER = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

# Telegram отдаёт ботам файлы не больше 20 МБ — всё, что больше,
# бот физически не сможет скачать.
MAX_UPLOAD_BYTES = 20 * 1024 * 1024


class DocxError(Exception):
    """Файл не является читаемым .docx."""


def is_docx_name(filename: str) -> bool:
    return (filename or "").lower().endswith(".docx")


def strip_media(src: Path, dst: Path) -> Tuple[int, int]:
    """Пересобирает .docx без картинок. Возвращает (было_байт, стало_байт)."""
    size_before = src.stat().st_size
    try:
        with zipfile.ZipFile(src) as zin:
            names = zin.namelist()
            if "word/document.xml" not in names:
                raise DocxError("В файле нет word/document.xml — это не .docx")
            dst.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
                for item in zin.infolist():
                    data = zin.read(item.filename)
                    if item.filename.startswith(MEDIA_PREFIX):
                        data = _PLACEHOLDER
                    info = zipfile.ZipInfo(item.filename, date_time=item.date_time)
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.external_attr = item.external_attr
                    zout.writestr(info, data)
    except zipfile.BadZipFile as exc:
        # Недописанный файл нельзя оставлять в uploads/: его будет заново
        # разбирать каждая пересборка, а удалить его преподавателю нечем
        dst.unlink(missing_ok=True)
        raise DocxError("Файл повреждён или не является .docx") from exc
    except Exception:
        dst.unlink(missing_ok=True)
        raise

    size_after = dst.stat().st_size
    logger.info(
        "strip_media: %s %.2f МБ → %.2f МБ",
        src.name, size_before / 1e6, size_after / 1e6,
    )
    return size_before, size_after


def media_share(path: Path) -> float:
    """Доля картинок в объёме файла — для диагностики."""
    try:
        with zipfile.ZipFile(path) as z:
            total = sum(i.compress_size for i in z.infolist()) or 1
            media = sum(
                i.compress_size for i in z.infolist() if i.filename.startswith(MEDIA_PREFIX)
            )
        return media / total
    except Exception:  # noqa: BLE001
        return 0.0


# Длина имени в папке uploads. Ограничение не от файловой системы,
# а чтобы пути оставались обозримыми в логах и на экране «Файлы».
MAX_STEM = 80
_DIGEST_LEN = 8


def safe_stem(filename: str) -> str:
    """Имя файла, пригодное для пути: без разделителей и служебных символов.

    Кириллицу оставляем — преподаватели называют файлы по-русски.

    Длинные имена не просто обрезаются: у двух похожих названий совпали бы
    первые 80 символов, второй файл лёг бы на место первого, и при ближайшей
    пересборке вопросы первого исчезли бы молча. Поэтому к обрезанному имени
    добавляется отпечаток полного — он не меняется от загрузки к загрузке,
    так что повторная присылка того же файла по-прежнему заменяет его
    вопросы, а не двоит.
    """
    stem = Path(filename or "").stem.strip()
    bad = '<>:"/\\|?*\0'
    cleaned = "".join("_" if ch in bad else ch for ch in stem)
    cleaned = cleaned.strip(". ")

    if not cleaned:
        return "file"
    if len(cleaned) <= MAX_STEM:
        return cleaned

    digest = hashlib.sha1(cleaned.encode("utf-8")).hexdigest()[:_DIGEST_LEN]
    keep = MAX_STEM - _DIGEST_LEN - 1
    return f"{cleaned[:keep].rstrip(' _')}_{digest}"


def list_uploads(uploads_dir: Path) -> List[Path]:
    if not uploads_dir.exists():
        return []
    return sorted(uploads_dir.glob("*.docx"))
