"""Загрузка билетов преподавателем: приём файла → разбор → тренажёр."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import tempfile
from pathlib import Path

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

import config
import subjects as subjects_cfg
from bot.keyboards.inline import (
    ALL_MENU_BUTTONS,
    BTN_BY_SECTION,
    MENU_BTN_UPLOAD,
    SEC_PREFIX,
    SHOW_MINE_CALLBACK,
    TOP_PREFIX,
    UNSORTED_KEY,
    UNSORTED_LABEL,
    WHOLE_SECTION_KEY,
    move_file_kb,
    questions_word,
    section_confirm_kb,
    sections_kb_for_upload,
    topics_kb_for_upload,
    uploaded_kb,
)
from bot.states.states import TeacherUpload
from database.db import get_user, set_teacher_content, set_teacher_setting
from database.models import ROLE_TEACHER
from services import (
    content_provider,
    docx_tools,
    pdf_tools,
    sections as sections_lib,
    storage,
    teacher_content,
)


logger = logging.getLogger(__name__)

router = Router()


UPLOAD_PROMPT = (
    "📥 Добавить вопросы\n\n"
    "Пришлите файл .docx или PDF со своими билетами — я разберу его\n"
    "и соберу тренажёр.\n\n"
    "Важно: в файле должен быть ключ с ответами — блок «Ответы» в конце, "
    "например «Часть А: А1 — 2; А2 — 3…».\n"
    "Без ключа я не смогу проверять ответы учеников.\n\n"
    "Можно прислать несколько файлов подряд."
)

# Что делаем с выбранным разделом. Флаг живёт в FSM, потому что кнопки
# разделов одни и те же в трёх местах: до файла, после файла и при разборе
# старых загрузок.
FLOW_START = "start"   # раздел выбран заранее, файла ещё нет
FLOW_FILE = "file"     # раздел для только что загруженного файла
FLOW_SORT = "sort"     # раскладываем то, что загружено раньше


PICK_SECTION_PROMPT = (
    "📥 Добавить вопросы\n\n"
    "Куда положить? Выберите раздел — потом пришлёте файл.\n"
    "Файл, где вопросы из разных разделов, — в «Смешанные вопросы»."
)

NEW_SECTION_PROMPT = (
    "Как назвать раздел?\n"
    "Пришлите название одним сообщением."
)

NEW_TOPIC_PROMPT = (
    "Как назвать тему?\n"
    "Пришлите название одним сообщением."
)


async def _show(message: Message, text: str, reply_markup=None) -> None:
    """Показывает шаг мастера, заменяя предыдущий, а не добавляя новый.

    Мастер — это один экран, который меняется: раздел, тема, итог. Отдельным
    сообщением на каждый шаг чат превращается в ленту, где до нужного места
    надо листать, а старые клавиатуры остаются живыми и по ним нажимают.

    Заменить можно только своё сообщение с текстом: когда шаг пришёл ответом
    на файл или на введённое название, редактировать нечего — тогда отправляем
    новое, и заменяться будет уже оно.
    """
    try:
        await message.edit_text(text, reply_markup=reply_markup)
        return
    except Exception as exc:  # noqa: BLE001
        # Тот же текст Telegram считает ошибкой; отправлять дубль не за чем
        if "not modified" in str(exc).lower():
            return
    await message.answer(text, reply_markup=reply_markup)


async def _teacher_subject(telegram_id: int) -> str | None:
    """Предмет преподавателя или None, если пользователь не преподаватель."""
    user = await get_user(telegram_id)
    if not user or user.role != ROLE_TEACHER:
        return None
    return user.current_subject or subjects_cfg.DEFAULT_SUBJECT


def _all_sections(telegram_id: int, subject: str) -> list:
    """Разделы программы плюс свои, добавленные этим преподавателем."""
    return sections_lib.merged(
        subject, teacher_content.custom_sections(telegram_id, subject)
    )


def _section_label(telegram_id: int, subject: str, key: str) -> str:
    return sections_lib.title_of(
        subject, key, teacher_content.custom_sections(telegram_id, subject)
    )


def _place_label(telegram_id: int, subject: str, key: str) -> str:
    """Подпись места: «раздел» или «раздел → тема». Одна на все экраны."""
    return sections_lib.place_title(
        subject,
        key,
        teacher_content.custom_sections(telegram_id, subject),
        teacher_content.custom_topics(telegram_id, subject),
    )


# ---------- Где ученик найдёт загруженное ----------
#
# Трое подряд загрузили вопросы и не нашли их: у одной стояли «только
# общие материалы», у другого тест лежал на уровень глубже, в теме, а третий
# потерял файлы, молча уехавшие в раздел, выбранный накануне. Отсюда три
# правила ниже: отчёт называет путь ученика, предупреждает о настройке
# и не кладёт файл по старому выбору.

# Сколько держится выбранный раздел, когда файлы присылают прямо в чат.
# Для пачки файлов этого с запасом, а вчерашний выбор уже не действует:
# состояние бота хранится на диске до двух суток, и без срока файл через день
# молча ложился туда, куда клали вчера.
STICKY_MINUTES = 30

HIDDEN_WARNING = (
    "⚠️ Ученики этих вопросов пока не видят: "
    "в настройках выбраны «Только общие материалы»."
)
SHOWN_NOTE = "✅ Теперь ученики видят ваши материалы."


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def _is_fresh(value) -> bool:
    """Выбран ли раздел недавно — в пределах одной пачки файлов."""
    try:
        chosen_at = dt.datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return False
    return _utcnow() - chosen_at <= dt.timedelta(minutes=STICKY_MINUTES)


def student_path(telegram_id: int, subject: str, key: str) -> str:
    """Путь ученика до места — теми же подписями, что на его кнопках."""
    parts = [subjects_cfg.MODE_LABELS["tests"], BTN_BY_SECTION]
    if not key:
        parts.append(UNSORTED_LABEL)
    else:
        parts.append(_section_label(telegram_id, subject, sections_lib.section_of(key)))
        if sections_lib.is_topic(key):
            parts.append(sections_lib.topic_title(
                subject, key, teacher_content.custom_topics(telegram_id, subject)
            ))
    return " → ".join(parts)


async def _hidden_from_students(telegram_id: int, subject: str) -> bool:
    """Ученики не видят материалы преподавателя: выбраны только общие."""
    user = await get_user(telegram_id)
    if not user:
        return False
    source = await content_provider.effective_source(user, subject)
    return source == subjects_cfg.SOURCE_BASE


async def _where_to_find(telegram_id: int, subject: str, key: str) -> tuple[str, bool]:
    """Хвост отчёта: где ученик найдёт вопросы и видит ли он их вообще."""
    hidden = await _hidden_from_students(telegram_id, subject)
    text = "Ученики найдут их так:\n" + student_path(telegram_id, subject, key)
    if hidden:
        text += f"\n\n{HIDDEN_WARNING}"
    return text, hidden


def _accepted_of(telegram_id: int, subject: str, filename: str) -> int:
    for item in teacher_content.load_manifest(telegram_id, subject).get("files") or []:
        if str(item.get("filename")) == filename:
            return int(item.get("accepted") or 0)
    return 0


async def _start_upload(message: Message, state: FSMContext, subject: str) -> None:
    """Начало загрузки: сперва раздел, потом файл.

    Если у предмета нет сетки разделов — сразу просим файл, лишний экран
    ни о чём преподавателю не нужен.
    """
    if not subjects_cfg.has_sections(subject):
        await state.set_state(TeacherUpload.waiting_file)
        await message.answer(UPLOAD_PROMPT)
        return

    await state.set_state(TeacherUpload.waiting_section)
    await state.update_data(sec_flow=FLOW_START, sec_target=None)
    await message.answer(
        PICK_SECTION_PROMPT,
        reply_markup=sections_kb_for_upload(
            _all_sections(message.chat.id, subject), allow_none=True
        ),
    )


@router.message(F.text == MENU_BTN_UPLOAD)
async def upload_entry(message: Message, state: FSMContext) -> None:
    subject = await _teacher_subject(message.from_user.id)
    if not subject:
        await message.answer("Этот раздел доступен преподавателям.")
        return
    if not subjects_cfg.teacher_can_upload(subject, "tests"):
        await message.answer(
            f"Для предмета «{subjects_cfg.subject_name(subject)}» загрузка билетов пока не подключена."
        )
        return

    await _start_upload(message, state, subject)


@router.callback_query(lambda c: c.data == "trainer:upload")
async def upload_from_trainer(callback: CallbackQuery, state: FSMContext) -> None:
    """Тот же приём файла, но вызванный кнопкой с экрана тренажёра."""
    subject = await _teacher_subject(callback.from_user.id)
    await callback.answer()
    if not subject:
        return
    if not subjects_cfg.teacher_can_upload(subject, "tests"):
        # Молча ничего не делать нельзя — кнопка выглядит сломанной
        await callback.message.answer(
            f"Для предмета «{subjects_cfg.subject_name(subject)}» "
            "загрузка билетов пока не подключена."
        )
        return
    await _start_upload(callback.message, state, subject)


def _format_report(
    result: teacher_content.RebuildResult,
    last_file: str,
    section_label: str | None = None,
) -> str:
    """Отчёт о разборе одного файла.

    Только про этот файл. Состояние всего тренажёра — счётчики, прочие
    файлы без ключа — живёт на экране «Мой тренажёр»: при загрузке пачки
    эта сводка повторялась после каждого файла и заслоняла главное.
    """
    lines: list[str] = []

    last = next((f for f in result.files if f.filename == last_file), None)

    if last is None:
        lines.append("Файл сохранён, но разобрать его не удалось.")
    elif last.found == 0:
        lines.append(
            f"❌ В файле «{last.filename}» не нашлось вопросов.\n\n"
            "Проверьте, что вопросы пронумерованы как А1., А2., В1. — "
            "именно по такому маркеру я их узнаю."
        )
    elif last.has_no_key:
        lines.append(
            f"⚠️ Нашёл {last.found} вопросов в «{last.filename}», "
            "но в файле нет ключа с ответами.\n\n"
            "Без ответов тренажёр не сможет проверять учеников, поэтому "
            "эти вопросы пока не подключены.\n"
            "Добавьте в конец файла блок «Ответы» и пришлите его снова."
        )
    else:
        lines.append(f"✅ Файл «{last.filename}» разобран: принято {last.accepted} из {last.found}.")
        if section_label:
            lines.append(f"Куда: {section_label}")
        if last.rejected:
            reasons = "\n".join(f"  • {r} — {n}" for r, n in last.reasons.items())
            lines.append(f"\nНе принято {last.rejected}:\n{reasons}")

    return "\n".join(lines)


# Ловим документ и в состоянии ожидания, и просто присланный в чат:
# преподаватель часто кидает файл, не заходя в раздел.
@router.message(F.document)
async def handle_document(message: Message, state: FSMContext) -> None:
    subject = await _teacher_subject(message.from_user.id)
    if not subject:
        # Ученикам файлы не нужны — молча не мешаем остальным хендлерам
        return

    # Та же проверка, что и на входе в раздел: файл можно прислать в чат
    # напрямую, минуя кнопку, и политика предмета не должна обходиться
    if not subjects_cfg.teacher_can_upload(subject, "tests"):
        await message.answer(
            f"Для предмета «{subjects_cfg.subject_name(subject)}» "
            "загрузка билетов пока не подключена."
        )
        return

    doc = message.document
    if not docx_tools.is_supported_name(doc.file_name or ""):
        await message.answer(
            "Нужен файл .docx или PDF с текстом.\n"
            "Фотографии и сканы бот пока не распознаёт."
        )
        return

    if (doc.file_size or 0) > docx_tools.MAX_UPLOAD_BYTES:
        await message.answer(
            f"Файл слишком большой ({doc.file_size / 1e6:.1f} МБ). "
            f"Telegram отдаёт ботам файлы до {docx_tools.MAX_UPLOAD_BYTES // (1024 * 1024)} МБ.\n"
            "Обычно вес дают картинки — попробуйте сохранить документ без "
            "иллюстраций или разделить его на части."
        )
        return

    telegram_id = message.from_user.id

    # Раздел, выбранный в этой сессии загрузки. Пока он держится, следующие
    # файлы кладутся туда же и вопрос не повторяется — так грузят пачками.
    # Держится он полчаса: вчерашний выбор молча уводил файлы не туда.
    # Если же преподаватель выбрал место и бот ждёт файл — место действует,
    # сколько бы времени ни прошло.
    data = await state.get_data()
    session_section = data.get("section")
    waiting = await state.get_state() == TeacherUpload.waiting_file.state
    if session_section is not None and not waiting and not _is_fresh(data.get("section_at")):
        session_section = None

    # Файл с тем же именем заменяет прежний. Если прежний лежал в другом
    # месте, молча переносить нельзя — его раздел опустеет без объяснений
    name = teacher_content.upload_name(doc.file_name or "")
    previous = teacher_content.file_sections(telegram_id, subject).get(name)
    moving = (
        previous is not None
        and session_section is not None
        and previous != session_section
    )

    status = await message.answer("⏳ Читаю файл…")

    tmp_dir = Path(tempfile.mkdtemp(prefix="aida_upload_"))
    tmp_path = tmp_dir / f"incoming{docx_tools.suffix_of(doc.file_name or '') or '.docx'}"

    try:
        await message.bot.download(doc, destination=tmp_path)

        target, size_before, size_after = await asyncio.to_thread(
            teacher_content.store_upload, telegram_id, subject, tmp_path, doc.file_name
        )

        await status.edit_text("⏳ Разбираю билеты…")
        if moving:
            # Пока преподаватель не решил — файл остаётся, где был
            assign = {target.name: previous}
        elif session_section is not None:
            assign = {target.name: session_section}
        else:
            assign = None
        result = await asyncio.to_thread(
            teacher_content.rebuild, telegram_id, subject, assign
        )

        await set_teacher_content(
            telegram_id, subject, "tests",
            path=str(storage.teacher_tests_path(telegram_id, subject)),
            items_count=result.total_accepted,
            status="ready" if result.total_accepted else "empty",
        )

        await status.delete()

        # Про вычистку картинок преподавателю знать незачем — это наша кухня.
        # Размеры до и после остаются в логах и манифесте.
        logger.info(
            "upload %s: %.2f → %.2f МБ", target.name, size_before / 1e6, size_after / 1e6
        )

        accepted = next(
            (f.accepted for f in result.files if f.filename == target.name), 0
        )
        # Знакомый файл спрашивать не о чем: место у него уже есть
        ask_section = (
            session_section is None
            and previous is None
            and accepted > 0
            and subjects_cfg.has_sections(subject)
        )
        token = teacher_content.file_token(target.name)

        if ask_section:
            await _ask_section(message, state, subject, result, target.name)
            return

        # Состояние снимаем, а выбранный раздел оставляем: следующий
        # файл ляжет туда же, но подсказка «жду файл» больше не мешает
        await state.set_state(None)
        if session_section is not None:
            await state.update_data(section_at=_utcnow().isoformat())

        if moving:
            old_label = _place_label(telegram_id, subject, previous)
            new_label = _place_label(telegram_id, subject, session_section)
            hidden = await _hidden_from_students(telegram_id, subject)
            text = (
                f"{_format_report(result, target.name, old_label)}\n\n"
                f"Этот файл уже лежал здесь: {old_label}. Я обновил его на месте.\n"
                f"Перенести в «{new_label}»?"
            )
            if hidden and accepted:
                text += f"\n\n{HIDDEN_WARNING}"
            await message.answer(text, reply_markup=move_file_kb(
                token, session_section, new_label, old_label,
                show_mine=hidden and accepted > 0,
            ))
            return

        place = session_section if session_section is not None else previous
        label = _place_label(telegram_id, subject, place) if place is not None else None
        report = _format_report(result, target.name, label)
        hidden = False
        if accepted and place is not None:
            where, hidden = await _where_to_find(telegram_id, subject, place)
            report += f"\n\n{where}"
        await message.answer(
            report,
            reply_markup=uploaded_kb(label, token, show_mine=hidden),
        )

    except (docx_tools.DocxError, pdf_tools.PdfError) as exc:
        await status.delete()
        await message.answer(f"Не смог прочитать файл: {exc}")
    except Exception:  # noqa: BLE001
        logger.exception("Ошибка при загрузке билетов от %s", telegram_id)
        try:
            await status.delete()
        except Exception:  # noqa: BLE001
            pass
        await message.answer("Что-то пошло не так при обработке файла. Попробуйте ещё раз.")
    finally:
        for p in (tmp_path,):
            try:
                p.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass
        try:
            tmp_dir.rmdir()
        except Exception:  # noqa: BLE001
            pass


# ---------- Выбор раздела ----------

async def _ask_section(
    message: Message,
    state: FSMContext,
    subject: str,
    result: teacher_content.RebuildResult,
    filename: str,
) -> None:
    """Спрашивает раздел для только что разобранного файла."""
    telegram_id = message.from_user.id
    await state.set_state(TeacherUpload.waiting_section)
    await state.update_data(sec_flow=FLOW_FILE, sec_target=filename)

    report = _format_report(result, filename)
    suggested = sections_lib.suggest(filename, subject)

    if suggested:
        label = _section_label(telegram_id, subject, suggested)
        await message.answer(
            f"{report}\n\nПохоже на раздел «{label}». Верно?",
            reply_markup=section_confirm_kb(suggested, label),
        )
        return

    await message.answer(
        f"{report}\n\nВ какой раздел это положить?",
        reply_markup=sections_kb_for_upload(_all_sections(telegram_id, subject)),
    )


async def _next_unsorted(
    message: Message,
    state: FSMContext,
    subject: str,
    note: str = "",
) -> bool:
    """Показывает следующий файл без раздела. False — раскладывать больше нечего.

    `note` — что случилось с предыдущим файлом. Он показывается здесь же,
    а не отдельным сообщением: иначе раскладка десяти файлов оставляет
    в чате двадцать сообщений, из которых девятнадцать уже не нужны.

    Пропущенные файлы запоминаются на время разбора: без этого «Пропустить»
    возвращало бы тот же файл по кругу — раздел-то у него так и не появился.
    """
    telegram_id = message.chat.id
    data = await state.get_data()
    skipped = set(data.get("sec_skipped") or [])

    pending = [
        name
        for name in teacher_content.files_without_section(telegram_id, subject)
        if name not in skipped
    ]
    if not pending:
        await state.set_state(None)
        await state.update_data(sec_flow=None, sec_target=None, sec_skipped=None)
        return False

    filename = pending[0]
    accepted = next(
        (
            item.get("accepted") or 0
            for item in teacher_content.load_manifest(telegram_id, subject).get("files") or []
            if str(item.get("filename")) == filename
        ),
        0,
    )

    await state.set_state(TeacherUpload.waiting_section)
    await state.update_data(sec_flow=FLOW_SORT, sec_target=filename)

    left = f"Осталось файлов: {len(pending)}\n\n" if len(pending) > 1 else ""
    head = f"{note}\n\n" if note else ""
    await _show(
        message,
        f"🗂 Разложить по разделам\n\n"
        f"{head}{left}«{filename}» — {accepted} {questions_word(accepted)}\n"
        f"В какой раздел?",
        reply_markup=sections_kb_for_upload(_all_sections(telegram_id, subject)),
    )
    return True


@router.callback_query(lambda c: c.data == "trainer:sort")
async def sort_entry(callback: CallbackQuery, state: FSMContext) -> None:
    """Разложить по разделам то, что загружено раньше."""
    subject = await _teacher_subject(callback.from_user.id)
    await callback.answer()
    if not subject:
        return
    # Новый заход — пропущенные в прошлый раз файлы снова в очереди
    await state.update_data(sec_skipped=None)
    if not await _next_unsorted(callback.message, state, subject):
        await _show(callback.message, "Все файлы уже разложены.")


@router.callback_query(lambda c: (c.data or "").startswith(f"{SEC_PREFIX}:"))
async def section_chosen(callback: CallbackQuery, state: FSMContext) -> None:
    subject = await _teacher_subject(callback.from_user.id)
    await callback.answer()
    if not subject:
        return

    telegram_id = callback.from_user.id
    action = (callback.data or "").split(":", 1)[1]
    data = await state.get_data()
    flow = data.get("sec_flow") or FLOW_FILE
    target = data.get("sec_target")

    # Развернуть полный список — из подсказки или из «Другой раздел».
    # У второй кнопки есть ярлык файла: она перекладывает именно тот файл,
    # под отчётом которого её нажали, даже если после него грузили другие.
    if action.startswith("list"):
        parts = action.split(":", 1)
        if len(parts) == 2:
            named = await asyncio.to_thread(
                teacher_content.find_file_by_token, telegram_id, subject, parts[1]
            )
            if not named:
                await _show(callback.message, "Этого файла больше нет.")
                return
            target = named
            flow = FLOW_FILE
            await state.update_data(sec_flow=flow, sec_target=target)

        await state.set_state(TeacherUpload.waiting_section)
        await _show(
            callback.message,
            f"«{target}» — в какой раздел?" if target else PICK_SECTION_PROMPT,
            reply_markup=sections_kb_for_upload(
                _all_sections(telegram_id, subject),
                none_text=UNSORTED_LABEL,
            ),
        )
        return

    if action == "new":
        await state.set_state(TeacherUpload.waiting_section_title)
        await _show(callback.message, NEW_SECTION_PROMPT)
        return

    # «Смешанные вопросы» при разборе старых загрузок: раздела у файла и так
    # нет, записывать нечего — помечаем разобранным и идём дальше. Без этой
    # пометки тот же файл возвращался бы по кругу.
    if action == UNSORTED_KEY and flow == FLOW_SORT:
        skipped = list(data.get("sec_skipped") or [])
        if target:
            skipped.append(target)
        await state.update_data(sec_skipped=skipped)
        note = f"«{target}» → {UNSORTED_LABEL}"
        if not await _next_unsorted(callback.message, state, subject, note=note):
            await _show(
                callback.message,
                f"{note}\n\nГотово, все файлы разложены.",
                reply_markup=uploaded_kb(),
            )
        return

    key = "" if action == UNSORTED_KEY else action
    custom = teacher_content.custom_sections(telegram_id, subject)
    if not sections_lib.is_valid(subject, key, custom):
        await _show(callback.message, "Такого раздела нет. Выберите из списка.")
        return

    # У «Смешанных вопросов» темы нет по определению: там вопросы разных
    # разделов вперемешку, и делить их на темы нечем.
    if key:
        await _ask_topic(callback.message, state, subject, flow, target, key)
        return

    await _apply_place(callback.message, state, subject, flow, target, key)


# ---------- Выбор темы ----------

async def _stale_click(message: Message, flow: str, target: str | None) -> bool:
    """Нажали кнопку из старого сообщения, а файла в работе уже нет.

    Клавиатуры живут в чате вечно, и по ним нажимают спустя часы. Молча
    провести такое нажатие нельзя: преподаватель решит, что переложил файл,
    а переложился бы чужой или ничей.
    """
    if flow == FLOW_START or target:
        return False
    await _show(
        message,
        "Этот файл уже разложен.\n"
        "Следующий можно прислать прямо в чат.",
        reply_markup=uploaded_kb(),
    )
    return True


async def _ask_topic(
    message: Message,
    state: FSMContext,
    subject: str,
    flow: str,
    target: str | None,
    section: str,
) -> None:
    """Второй шаг: тема внутри выбранного раздела."""
    if await _stale_click(message, flow, target):
        return

    telegram_id = message.chat.id
    label = _section_label(telegram_id, subject, section)
    topics = sections_lib.merged_topics(
        subject, section, teacher_content.custom_topics(telegram_id, subject)
    )

    await state.set_state(TeacherUpload.waiting_topic)
    await state.update_data(sec_flow=flow, sec_target=target, sec_section=section)

    head = f"«{target}»\n\n" if target and flow != FLOW_START else ""
    await _show(
        message,
        f"{head}Раздел: {label}\n\nТеперь тема.",
        reply_markup=topics_kb_for_upload(topics),
    )


@router.callback_query(lambda c: (c.data or "").startswith(f"{TOP_PREFIX}:"))
async def topic_chosen(callback: CallbackQuery, state: FSMContext) -> None:
    subject = await _teacher_subject(callback.from_user.id)
    await callback.answer()
    if not subject:
        return

    telegram_id = callback.from_user.id
    action = (callback.data or "").split(":", 1)[1]
    data = await state.get_data()
    flow = data.get("sec_flow") or FLOW_FILE
    target = data.get("sec_target")
    section = data.get("sec_section") or ""

    if action == "back":
        await state.set_state(TeacherUpload.waiting_section)
        await _show(
            callback.message,
            f"«{target}» — в какой раздел?" if target else PICK_SECTION_PROMPT,
            reply_markup=sections_kb_for_upload(_all_sections(telegram_id, subject)),
        )
        return

    if action == "new":
        await state.set_state(TeacherUpload.waiting_topic_title)
        await _show(callback.message, NEW_TOPIC_PROMPT)
        return

    # «Весь раздел» — файл ложится в раздел без темы. Так и должно быть
    # у полных билетов по разделу: тема у них не одна.
    if action == WHOLE_SECTION_KEY:
        await _apply_place(callback.message, state, subject, flow, target, section)
        return

    custom_topics = teacher_content.custom_topics(telegram_id, subject)
    custom = teacher_content.custom_sections(telegram_id, subject)
    if not sections_lib.is_valid(subject, action, custom, custom_topics):
        await _show(callback.message, "Такой темы нет. Выберите из списка.")
        return

    await _apply_place(callback.message, state, subject, flow, target, action)


@router.message(
    TeacherUpload.waiting_topic_title,
    ~F.text.in_(ALL_MENU_BUTTONS),
    ~F.text.startswith("/"),
)
async def topic_title_entered(message: Message, state: FSMContext) -> None:
    """Название своей темы."""
    subject = await _teacher_subject(message.from_user.id)
    if not subject:
        return

    data = await state.get_data()
    section = data.get("sec_section") or ""
    if not section:
        await message.answer("Сначала выберите раздел.")
        return

    key = await asyncio.to_thread(
        teacher_content.add_custom_topic,
        message.from_user.id, subject, section, message.text or "",
    )
    if not key:
        await message.answer("Не понял название. Пришлите его одним сообщением.")
        return

    await _apply_place(
        message, state, subject,
        data.get("sec_flow") or FLOW_FILE,
        data.get("sec_target"),
        key,
    )


@router.message(
    TeacherUpload.waiting_topic,
    ~F.text.in_(ALL_MENU_BUTTONS),
    ~F.text.startswith("/"),
)
async def waiting_topic_hint(message: Message, state: FSMContext) -> None:
    await message.answer("Выберите тему кнопкой выше.")


async def _apply_place(
    message: Message,
    state: FSMContext,
    subject: str,
    flow: str,
    target: str | None,
    key: str,
) -> None:
    """Записывает выбранное место и ведёт дальше по тому потоку, из которого пришли.

    «Место» — раздел или тема внутри него: для всего, что ниже, разницы нет,
    в поле «Раздел» строки лежит один ключ.
    """
    telegram_id = message.chat.id
    label = _place_label(telegram_id, subject, key)

    if await _stale_click(message, flow, target):
        return

    if flow == FLOW_START:
        # Раздел выбран до файла — запоминаем и ждём документ
        await state.set_state(TeacherUpload.waiting_file)
        await state.update_data(
            section=key, section_at=_utcnow().isoformat(),
            sec_flow=None, sec_target=None, sec_section=None,
        )
        await _show(
            message,
            f"Куда: {label}\n\n"
            "Пришлите файл .docx или PDF — всё, что в нём найдётся, попадёт сюда.\n"
            "Можно прислать несколько файлов подряд."
        )
        return

    if target:
        await asyncio.to_thread(
            teacher_content.set_file_section, telegram_id, subject, target, key
        )

    if flow == FLOW_SORT:
        note = f"«{target}» → {label}"
        if not await _next_unsorted(message, state, subject, note=note):
            await _show(
                message,
                f"{note}\n\nГотово, все файлы разложены.",
                reply_markup=uploaded_kb(),
            )
        return

    # Загрузка: запоминаем выбор на остаток пачки
    await state.set_state(None)
    await state.update_data(
        section=key, section_at=_utcnow().isoformat(), sec_flow=None, sec_target=None
    )

    lines = [f"Готово: «{target}» → {label}"]
    hidden = False
    if _accepted_of(telegram_id, subject, target):
        where, hidden = await _where_to_find(telegram_id, subject, key)
        lines.append(where)
    lines.append(
        f"Файлы, присланные в ближайшие {STICKY_MINUTES} минут, положу туда же."
    )
    await _show(
        message,
        "\n\n".join(lines),
        reply_markup=uploaded_kb(
            label, teacher_content.file_token(target), show_mine=hidden
        ),
    )


@router.message(
    TeacherUpload.waiting_section_title,
    ~F.text.in_(ALL_MENU_BUTTONS),
    ~F.text.startswith("/"),
)
async def section_title_entered(message: Message, state: FSMContext) -> None:
    """Название своего раздела."""
    subject = await _teacher_subject(message.from_user.id)
    if not subject:
        return

    key = await asyncio.to_thread(
        teacher_content.add_custom_section, message.from_user.id, subject, message.text or ""
    )
    if not key:
        await message.answer("Не понял название. Пришлите его одним сообщением.")
        return

    # Свой раздел тоже проходит шаг темы: в нём тем ещё нет, но завести
    # свою можно сразу — иначе новый раздел оказался бы урезанным
    # по сравнению с разделами программы.
    data = await state.get_data()
    await _ask_topic(
        message, state, subject,
        data.get("sec_flow") or FLOW_FILE,
        data.get("sec_target"),
        key,
    )


@router.message(
    TeacherUpload.waiting_section,
    ~F.text.in_(ALL_MENU_BUTTONS),
    ~F.text.startswith("/"),
)
async def waiting_section_hint(message: Message, state: FSMContext) -> None:
    await message.answer("Выберите раздел кнопкой выше.")


@router.message(
    TeacherUpload.waiting_file,
    ~F.text.in_(ALL_MENU_BUTTONS),
    ~F.text.startswith("/"),
)
async def waiting_file_hint(message: Message, state: FSMContext) -> None:
    """Подсказка, пока ждём файл.

    Нажатия кнопок меню и команды сюда не попадают — иначе из режима
    ожидания было бы не выйти: он перехватывал бы вообще всё.
    """
    await message.answer(
        "Жду файл с билетами — .docx или PDF.\n"
        "Чтобы выйти — нажмите любую кнопку меню или отправьте /menu.",
    )


# ---------- Кнопки под отчётом о загрузке ----------

def _without_show_mine(markup) -> InlineKeyboardMarkup | None:
    """Та же клавиатура, но без кнопки «Показывать ученикам мои материалы»."""
    if not markup:
        return None
    rows = [
        row for row in markup.inline_keyboard
        if not any(button.callback_data == SHOW_MINE_CALLBACK for button in row)
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


@router.callback_query(F.data == SHOW_MINE_CALLBACK)
async def show_mine(callback: CallbackQuery) -> None:
    """Переключить материалы тренажёра на свои прямо из отчёта о загрузке.

    Выбираем «мои, а пока их нет — общие»: свои у преподавателя уже есть,
    ученики увидят именно их, а если файлы когда-нибудь удалят — тренажёр
    не опустеет, а вернётся к общим вопросам.
    """
    subject = await _teacher_subject(callback.from_user.id)
    if not subject:
        await callback.answer()
        return

    allowed = subjects_cfg.allowed_sources(subject)
    value = (
        subjects_cfg.SOURCE_TEACHER_THEN_BASE
        if subjects_cfg.SOURCE_TEACHER_THEN_BASE in allowed
        else subjects_cfg.SOURCE_TEACHER
    )
    await set_teacher_setting(
        callback.from_user.id, subject, content_provider.MATERIALS_SOURCE, value
    )
    await callback.answer("Готово — ученики видят ваши материалы")

    text = (callback.message.text or "").replace(HIDDEN_WARNING, SHOWN_NOTE)
    try:
        await callback.message.edit_text(
            text, reply_markup=_without_show_mine(callback.message.reply_markup)
        )
    except Exception:  # noqa: BLE001
        # Сообщение слишком старое, чтобы его править, — настройка всё равно сохранена
        pass


@router.callback_query(
    lambda c: (c.data or "").startswith(("upl:move:", "upl:keep:"))
)
async def move_decision(callback: CallbackQuery, state: FSMContext) -> None:
    """Файл с тем же именем уже лежал в другом месте: перенести или оставить."""
    subject = await _teacher_subject(callback.from_user.id)
    await callback.answer()
    if not subject:
        return

    telegram_id = callback.from_user.id
    parts = (callback.data or "").split(":", 3)
    action, token = parts[1], parts[2]
    new_key = parts[3] if len(parts) > 3 else ""

    filename = await asyncio.to_thread(
        teacher_content.find_file_by_token, telegram_id, subject, token
    )
    if not filename:
        await _show(callback.message, "Этого файла больше нет.")
        return

    if action == "move":
        custom = teacher_content.custom_sections(telegram_id, subject)
        custom_topics = teacher_content.custom_topics(telegram_id, subject)
        if new_key and not sections_lib.is_valid(subject, new_key, custom, custom_topics):
            await _show(callback.message, "Такого раздела больше нет. Выберите место заново.")
            return
        await asyncio.to_thread(
            teacher_content.set_file_section, telegram_id, subject, filename, new_key
        )
        place = new_key
        verb = "Перенёс"
    else:
        place = teacher_content.file_sections(telegram_id, subject).get(filename, "")
        verb = "Оставил"
        # Выбор «оставить там» — значит и следующие файлы туда, а не в новое
        await state.update_data(section=place, section_at=_utcnow().isoformat())

    label = _place_label(telegram_id, subject, place)
    lines = [f"{verb}: «{filename}» → {label}"]
    hidden = False
    if _accepted_of(telegram_id, subject, filename):
        where, hidden = await _where_to_find(telegram_id, subject, place)
        lines.append(where)
    await _show(
        callback.message,
        "\n\n".join(lines),
        reply_markup=uploaded_kb(label, token, show_mine=hidden),
    )


@router.message(Command("reparse"))
async def reparse_cmd(message: Message) -> None:
    """Перегоняет накопленные файлы текущей версией парсера.

    Нужно после доработок парсера: материалы преподавателей пересобираются
    из сохранённых оригиналов, перезаливка не требуется.
    """
    if config.ADMIN_IDS and message.from_user.id not in config.ADMIN_IDS:
        await message.answer("Нет доступа.")
        return

    teachers_root = storage.TEACHERS_ROOT
    if not teachers_root.exists():
        await message.answer("Загруженных материалов пока нет.")
        return

    await message.answer("Пересобираю материалы всех преподавателей…")

    total_teachers = 0
    total_rows = 0
    for teacher_dir in sorted(teachers_root.iterdir()):
        if not teacher_dir.is_dir() or not teacher_dir.name.isdigit():
            continue
        for subject_dir in sorted(teacher_dir.iterdir()):
            if not subject_dir.is_dir():
                continue
            tg_id = int(teacher_dir.name)
            subject = subject_dir.name
            result = await asyncio.to_thread(teacher_content.rebuild, tg_id, subject)
            if result.files:
                total_teachers += 1
                total_rows += result.total_accepted
                await set_teacher_content(
                    tg_id, subject, "tests",
                    path=str(storage.teacher_tests_path(tg_id, subject)),
                    items_count=result.total_accepted,
                    status="ready" if result.total_accepted else "empty",
                )

    await message.answer(
        f"Готово. Преподавателей: {total_teachers}, вопросов принято: {total_rows}."
    )
