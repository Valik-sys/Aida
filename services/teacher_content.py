"""Сборка тренажёра преподавателя из загруженных им файлов.

Ключевое решение: `tests.json` всегда пересобирается из папки `uploads/`
целиком, а не дописывается по кусочкам. Из этого следует:

  • повторная загрузка файла с тем же именем заменяет его вопросы, а не двоит;
  • новая версия парсера применяется ко всему накопленному одной командой,
    без просьб к преподавателям перезаливать материалы;
  • состояние на диске всегда воспроизводимо из оригиналов.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from services import docx_tools, sections as sections_lib, storage, ticket_parser


logger = logging.getLogger(__name__)


@dataclass
class FileReport:
    filename: str
    accepted: int
    rejected: int
    source: str
    reasons: Dict[str, int] = field(default_factory=dict)
    section: str = ""

    @property
    def found(self) -> int:
        return self.accepted + self.rejected

    @property
    def has_no_key(self) -> bool:
        """Вопросы нашлись, но все отбракованы из-за отсутствия ответов."""
        return (
            self.accepted == 0
            and self.found > 0
            and self.reasons.get(ticket_parser.REASON_NO_ANSWER, 0) == self.rejected
        )


@dataclass
class RebuildResult:
    files: List[FileReport] = field(default_factory=list)
    rows: List[Dict[str, str]] = field(default_factory=list)

    @property
    def total_accepted(self) -> int:
        return len(self.rows)

    @property
    def total_rejected(self) -> int:
        return sum(f.rejected for f in self.files)

    @property
    def total_found(self) -> int:
        return sum(f.found for f in self.files)

    def count_by_type(self) -> tuple[int, int]:
        with_options = sum(
            1 for r in self.rows if any(r.get(f"Вар.{i}") for i in range(1, 6))
        )
        return with_options, len(self.rows) - with_options

    def files_without_key(self) -> List[FileReport]:
        return [f for f in self.files if f.has_no_key]


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).isoformat()


def rebuild(
    tg_id: int,
    subject: str,
    assign: Optional[Dict[str, str]] = None,
) -> RebuildResult:
    """Пересобирает tests.json и manifest.json из всех файлов преподавателя.

    `assign` — раздел для только что загруженных файлов, {имя файла: ключ}.
    Разделы остальных файлов берутся из прежнего манифеста: разбор можно
    повторять сколько угодно, раскладка от этого не теряется.
    """
    uploads = docx_tools.list_uploads(storage.teacher_uploads_dir(tg_id, subject))
    result = RebuildResult()

    previous = load_manifest(tg_id, subject)
    sections_map = _sections_from(previous)
    sections_map.update(assign or {})
    custom = dict(previous.get("sections") or {})
    custom_topics_map = dict(previous.get("topics") or {})

    manifest_files: List[Dict[str, object]] = []
    rejected_all: List[Dict[str, str]] = []

    for path in uploads:
        section = sections_map.get(path.name, "")
        try:
            parsed = ticket_parser.parse_docx(path)
        except Exception:  # noqa: BLE001
            logger.exception("Не удалось разобрать %s", path)
            result.files.append(
                FileReport(filename=path.name, accepted=0, rejected=0, source="error")
            )
            manifest_files.append({
                "filename": path.name, "accepted": 0, "rejected": 0,
                "source": "error", "size_bytes": path.stat().st_size,
                "section": section,
            })
            continue

        for row in parsed.rows:
            row["Раздел"] = section

        result.rows.extend(parsed.rows)
        rejected_all.extend(
            {**r, "file": path.name} for r in parsed.rejected
        )

        report = FileReport(
            filename=path.name,
            accepted=parsed.accepted_count,
            rejected=parsed.rejected_count,
            source=parsed.source,
            reasons=parsed.reasons_summary(),
            section=section,
        )
        result.files.append(report)
        manifest_files.append({
            "filename": path.name,
            "variant": parsed.variant,
            "accepted": parsed.accepted_count,
            "rejected": parsed.rejected_count,
            "source": parsed.source,
            "reasons": parsed.reasons_summary(),
            "size_bytes": path.stat().st_size,
            "section": section,
        })

    storage.write_json(
        storage.teacher_tests_path(tg_id, subject),
        {
            "parser_version": ticket_parser.PARSER_VERSION,
            "subject": subject,
            "updated_at": _now(),
            "rows": result.rows,
        },
    )

    # Отбракованное не выбрасываем: по нему видно, что чинить в парсере,
    # и из него собирается уточняющий диалог с преподавателем.
    storage.write_json(
        storage.teacher_manifest_path(tg_id, subject),
        {
            "parser_version": ticket_parser.PARSER_VERSION,
            "updated_at": _now(),
            "sections": custom,
            "topics": custom_topics_map,
            "files": manifest_files,
            "accepted_total": result.total_accepted,
            "rejected_total": result.total_rejected,
            "rejected": rejected_all[:500],
        },
    )

    logger.info(
        "rebuild teacher=%s subject=%s: файлов=%d принято=%d отбраковано=%d",
        tg_id, subject, len(uploads), result.total_accepted, result.total_rejected,
    )
    return result


def load_tests(tg_id: int, subject: str) -> List[Dict[str, str]]:
    """Строки тренажёра преподавателя. Пустой список, если ничего не загружено."""
    data = storage.read_json(storage.teacher_tests_path(tg_id, subject), default=None)
    if not data:
        return []
    return data.get("rows", [])


def load_tests_document(tg_id: int, subject: str) -> Dict[str, object]:
    """Весь tests.json целиком — нужен, когда важны не только строки.

    Например, дата последней пересборки для экрана «Мой тренажёр».
    """
    return storage.read_json(storage.teacher_tests_path(tg_id, subject), default={}) or {}


def files_overview(tg_id: int, subject: str) -> List[Dict[str, object]]:
    """Опись загруженного: файл, сколько дал вопросов и что с ним не так.

    Нужна, чтобы преподаватель видел, что уже есть, и не загружал то же
    самое по второму кругу.
    """
    manifest = load_manifest(tg_id, subject)
    overview: List[Dict[str, object]] = []

    for item in manifest.get("files") or []:
        accepted = item.get("accepted") or 0
        rejected = item.get("rejected") or 0
        reasons = item.get("reasons") or {}

        if accepted:
            problem = ""
        elif accepted + rejected == 0:
            problem = "не нашлось вопросов"
        elif reasons.get(ticket_parser.REASON_NO_ANSWER) == rejected:
            problem = "нет ключа с ответами"
        else:
            problem = next(iter(reasons), "не удалось разобрать")

        overview.append({
            "filename": item.get("filename"),
            "accepted": accepted,
            "problem": problem,
        })

    return overview


def problem_files(tg_id: int, subject: str) -> Dict[str, List[Dict[str, object]]]:
    """Файлы, из которых не удалось взять ни одного вопроса, по причинам.

    Разделяем два случая: ключа с ответами нет (вопросы нашлись, но проверять
    их нечем) и вопросов не нашлось вовсе (не тот формат или не та разметка) —
    преподавателю нужны разные действия.
    """
    manifest = load_manifest(tg_id, subject)
    files = manifest.get("files") or []

    no_key: List[Dict[str, object]] = []
    no_questions: List[Dict[str, object]] = []
    other: List[Dict[str, object]] = []

    for item in files:
        if item.get("accepted"):
            continue
        found = (item.get("accepted") or 0) + (item.get("rejected") or 0)
        reasons = item.get("reasons") or {}
        if found == 0:
            no_questions.append(item)
        elif reasons.get(ticket_parser.REASON_NO_ANSWER) == item.get("rejected"):
            no_key.append({**item, "found": found})
        else:
            other.append({**item, "found": found})

    return {"no_key": no_key, "no_questions": no_questions, "other": other}


def load_flashcards(tg_id: int, subject: str) -> Dict[str, List[Dict[str, str]]]:
    """Карточки преподавателя. Пока их загружать нечем — файла обычно нет.

    Формат тот же, что у общего слоя: категория → список карточек.
    """
    data = storage.read_json(storage.teacher_flashcards_path(tg_id, subject), default=None)
    if not isinstance(data, dict):
        return {}
    return {
        "dates": data.get("dates") or [],
        "concepts": data.get("concepts") or [],
        "persons": data.get("persons") or [],
    }


def load_manifest(tg_id: int, subject: str) -> Dict[str, object]:
    return storage.read_json(storage.teacher_manifest_path(tg_id, subject), default={}) or {}


# ---------- Разделы ----------

def _sections_from(manifest: Dict[str, object]) -> Dict[str, str]:
    """Раскладка файлов по разделам из манифеста: {имя файла: ключ}."""
    out: Dict[str, str] = {}
    for item in manifest.get("files") or []:
        name = item.get("filename")
        if name:
            out[str(name)] = str(item.get("section") or "")
    return out


def file_sections(tg_id: int, subject: str) -> Dict[str, str]:
    return _sections_from(load_manifest(tg_id, subject))


def custom_sections(tg_id: int, subject: str) -> Dict[str, str]:
    """Разделы, добавленные преподавателем сверх программы."""
    data = load_manifest(tg_id, subject).get("sections") or {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def add_custom_section(tg_id: int, subject: str, title: str) -> Optional[str]:
    """Заводит свой раздел и возвращает его ключ. None — если название пустое.

    Одинаковые названия не плодим: повторный ввод отдаёт тот же ключ, иначе
    в списке появятся два «Обобщение», между которыми не выбрать.
    """
    title = sections_lib.clean_title(title)
    if not title:
        return None

    manifest = load_manifest(tg_id, subject)
    custom = {str(k): str(v) for k, v in (manifest.get("sections") or {}).items()}

    for key, existing in custom.items():
        if existing.casefold() == title.casefold():
            return key

    key = sections_lib.next_custom_key(custom)
    custom[key] = title
    manifest["sections"] = custom
    storage.write_json(storage.teacher_manifest_path(tg_id, subject), manifest)
    return key


def custom_topics(tg_id: int, subject: str) -> Dict[str, str]:
    """Темы, добавленные преподавателем сверх программы, по всем разделам.

    Ключ несёт в себе раздел («6_c1»), поэтому отдельного поля не нужно
    и одна плоская таблица обслуживает все разделы сразу.
    """
    data = load_manifest(tg_id, subject).get("topics") or {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def add_custom_topic(tg_id: int, subject: str, section: str, title: str) -> Optional[str]:
    """Заводит свою тему в разделе и возвращает её ключ. None — если пусто.

    Одинаковые названия внутри раздела не плодим — как и у разделов:
    повторный ввод отдаёт тот же ключ.
    """
    title = sections_lib.clean_title(title)
    if not title:
        return None

    manifest = load_manifest(tg_id, subject)
    topics = {str(k): str(v) for k, v in (manifest.get("topics") or {}).items()}

    for key, existing in topics.items():
        if sections_lib.section_of(key) == section and existing.casefold() == title.casefold():
            return key

    key = sections_lib.next_custom_topic_key(section, topics)
    topics[key] = title
    manifest["topics"] = topics
    storage.write_json(storage.teacher_manifest_path(tg_id, subject), manifest)
    return key


def set_file_section(tg_id: int, subject: str, filename: str, section: str) -> bool:
    """Перекладывает файл в раздел: правит манифест и строки тренажёра.

    Файлы не разбираются заново — меняется только поле «Раздел» у строк
    этого файла. Разбор занимает секунды на файл, а перекладывание должно
    быть мгновенным.
    """
    manifest = load_manifest(tg_id, subject)
    files = manifest.get("files") or []

    found = False
    for item in files:
        if str(item.get("filename")) == filename:
            item["section"] = section
            found = True
            break
    if not found:
        return False

    storage.write_json(storage.teacher_manifest_path(tg_id, subject), manifest)

    document = storage.read_json(storage.teacher_tests_path(tg_id, subject), default=None)
    if not document:
        return True

    variant = Path(filename).stem
    for row in document.get("rows") or []:
        if str(row.get("Вариант")) == variant:
            row["Раздел"] = section
    storage.write_json(storage.teacher_tests_path(tg_id, subject), document)
    return True


def file_token(filename: str) -> str:
    """Короткий ярлык файла для callback-данных.

    Имена файлов кириллические и длинные, в лимит Telegram в 64 байта
    не влезают. Ярлык нужен, чтобы кнопка указывала на конкретный файл,
    а не на «последний загруженный»: клавиатуры старых сообщений живут
    в чате вечно, и нажатие по ним не должно трогать чужой файл.
    """
    return hashlib.sha1((filename or "").encode("utf-8")).hexdigest()[:12]


def find_file_by_token(tg_id: int, subject: str, token: str) -> Optional[str]:
    """Имя файла по ярлыку. None — если такого файла уже нет."""
    if not token:
        return None
    for item in load_manifest(tg_id, subject).get("files") or []:
        name = str(item.get("filename") or "")
        if name and file_token(name) == token:
            return name
    return None


def files_without_section(tg_id: int, subject: str) -> List[str]:
    """Файлы, которым раздел не назначен. Пустые файлы не считаем.

    Файл без единого принятого вопроса раскладывать некуда и незачем —
    он всё равно не попадёт к ученику, а в списке будет мозолить глаза.
    """
    manifest = load_manifest(tg_id, subject)
    return [
        str(item.get("filename"))
        for item in manifest.get("files") or []
        if not item.get("section") and (item.get("accepted") or 0) > 0
    ]


def counts_by_section(tg_id: int, subject: str) -> Dict[str, int]:
    """Сколько вопросов лежит в каждом разделе. Ключ "" — без раздела."""
    counts: Dict[str, int] = {}
    for row in load_tests(tg_id, subject):
        key = str(row.get("Раздел") or "")
        counts[key] = counts.get(key, 0) + 1
    return counts


def store_upload(tg_id: int, subject: str, tmp_path: Path, original_name: str) -> tuple[Path, int, int]:
    """Кладёт файл в uploads/, вычистив картинки. Возвращает (путь, было, стало).

    Картинки вычищаются только из .docx: там это 94% веса, а разбор от них
    не зависит. PDF кладётся как есть — вынуть из него картинки, не сломав
    страницы, нельзя, а текст в нём и так лежит отдельно от изображений.
    """
    uploads_dir = storage.teacher_uploads_dir(tg_id, subject)
    uploads_dir.mkdir(parents=True, exist_ok=True)
    suffix = docx_tools.suffix_of(original_name) or ".docx"
    target = uploads_dir / f"{docx_tools.safe_stem(original_name)}{suffix}"

    if suffix == ".docx":
        before, after = docx_tools.strip_media(tmp_path, target)
        return target, before, after

    size = tmp_path.stat().st_size
    target.write_bytes(tmp_path.read_bytes())
    return target, size, size


def delete_upload(tg_id: int, subject: str, filename: str) -> bool:
    path = storage.teacher_uploads_dir(tg_id, subject) / filename
    if not path.exists():
        return False
    path.unlink()
    return True
