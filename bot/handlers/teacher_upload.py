"""Загрузка билетов преподавателем: приём .docx → разбор → тренажёр."""

from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import config
import subjects as subjects_cfg
from bot.keyboards.inline import (
    ALL_MENU_BUTTONS,
    MENU_BTN_UPLOAD,
    SEC_PREFIX,
    TOP_PREFIX,
    UNSORTED_KEY,
    UNSORTED_LABEL,
    WHOLE_SECTION_KEY,
    questions_word,
    section_confirm_kb,
    sections_kb_for_upload,
    topics_kb_for_upload,
    uploaded_kb,
)
from bot.states.states import TeacherUpload
from database.db import get_user, set_teacher_content
from database.models import ROLE_TEACHER
from services import docx_tools, sections as sections_lib, storage, teacher_content


logger = logging.getLogger(__name__)

router = Router()


UPLOAD_PROMPT = (
    "📥 Добавить вопросы\n\n"
    "Пришлите файл .docx со своими билетами — я разберу его и соберу тренажёр.\n\n"
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
            lines.append(f"Раздел: {section_label}")
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
    if not docx_tools.is_docx_name(doc.file_name or ""):
        await message.answer("Нужен файл в формате .docx. Другие форматы пока не поддерживаются.")
        return

    if (doc.file_size or 0) > docx_tools.MAX_UPLOAD_BYTES:
        await message.answer(
            f"Файл слишком большой ({doc.file_size / 1e6:.1f} МБ). "
            f"Telegram отдаёт ботам файлы до {docx_tools.MAX_UPLOAD_BYTES // (1024 * 1024)} МБ.\n"
            "Обычно вес дают картинки — попробуйте сохранить документ без иллюстраций."
        )
        return

    telegram_id = message.from_user.id

    # Раздел, выбранный в этой сессии загрузки. Пока он держится, следующие
    # файлы кладутся туда же и вопрос не повторяется — так грузят пачками.
    data = await state.get_data()
    session_section = data.get("section")

    status = await message.answer("⏳ Читаю файл…")

    tmp_dir = Path(tempfile.mkdtemp(prefix="aida_upload_"))
    tmp_path = tmp_dir / "incoming.docx"

    try:
        await message.bot.download(doc, destination=tmp_path)

        target, size_before, size_after = await asyncio.to_thread(
            teacher_content.store_upload, telegram_id, subject, tmp_path, doc.file_name
        )

        await status.edit_text("⏳ Разбираю билеты…")
        assign = {target.name: session_section} if session_section is not None else None
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
        ask_section = (
            session_section is None
            and accepted > 0
            and subjects_cfg.has_sections(subject)
        )

        if ask_section:
            await _ask_section(message, state, subject, result, target.name)
        else:
            label = (
                _section_label(telegram_id, subject, session_section)
                if session_section else None
            )
            report = _format_report(result, target.name, label)
            # Состояние снимаем, а выбранный раздел оставляем: следующий
            # файл ляжет туда же, но подсказка «жду файл» больше не мешает
            await state.set_state(None)
            await message.answer(
                report,
                reply_markup=uploaded_kb(
                    label, teacher_content.file_token(target.name)
                ),
            )

    except docx_tools.DocxError as exc:
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


async def _next_unsorted(message: Message, state: FSMContext, subject: str) -> bool:
    """Показывает следующий файл без раздела. False — раскладывать больше нечего.

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
    await message.answer(
        f"🗂 Разложить по разделам\n\n"
        f"{left}«{filename}» — {accepted} {questions_word(accepted)}\n"
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
        await callback.message.answer("Все файлы уже разложены.")


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
                await callback.message.answer("Этого файла больше нет.")
                return
            target = named
            flow = FLOW_FILE
            await state.update_data(sec_flow=flow, sec_target=target)

        await state.set_state(TeacherUpload.waiting_section)
        await callback.message.answer(
            f"«{target}» — в какой раздел?" if target else PICK_SECTION_PROMPT,
            reply_markup=sections_kb_for_upload(
                _all_sections(telegram_id, subject),
                none_text=UNSORTED_LABEL,
            ),
        )
        return

    if action == "new":
        await state.set_state(TeacherUpload.waiting_section_title)
        await callback.message.answer(NEW_SECTION_PROMPT)
        return

    # «Смешанные вопросы» при разборе старых загрузок: раздела у файла и так
    # нет, записывать нечего — помечаем разобранным и идём дальше. Без этой
    # пометки тот же файл возвращался бы по кругу.
    if action == UNSORTED_KEY and flow == FLOW_SORT:
        skipped = list(data.get("sec_skipped") or [])
        if target:
            skipped.append(target)
        await state.update_data(sec_skipped=skipped)
        await callback.message.answer(f"«{target}» → {UNSORTED_LABEL}")
        if not await _next_unsorted(callback.message, state, subject):
            await callback.message.answer(
                "Готово, все файлы разложены.", reply_markup=uploaded_kb()
            )
        return

    key = "" if action == UNSORTED_KEY else action
    custom = teacher_content.custom_sections(telegram_id, subject)
    if not sections_lib.is_valid(subject, key, custom):
        await callback.message.answer("Такого раздела нет. Выберите из списка.")
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
    await message.answer(
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

    await message.answer(
        f"Раздел: {label}\n\nТеперь тема.",
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
        await callback.message.answer(
            f"«{target}» — в какой раздел?" if target else PICK_SECTION_PROMPT,
            reply_markup=sections_kb_for_upload(_all_sections(telegram_id, subject)),
        )
        return

    if action == "new":
        await state.set_state(TeacherUpload.waiting_topic_title)
        await callback.message.answer(NEW_TOPIC_PROMPT)
        return

    # «Весь раздел» — файл ложится в раздел без темы. Так и должно быть
    # у полных билетов по разделу: тема у них не одна.
    if action == WHOLE_SECTION_KEY:
        await _apply_place(callback.message, state, subject, flow, target, section)
        return

    custom_topics = teacher_content.custom_topics(telegram_id, subject)
    custom = teacher_content.custom_sections(telegram_id, subject)
    if not sections_lib.is_valid(subject, action, custom, custom_topics):
        await callback.message.answer("Такой темы нет. Выберите из списка.")
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
            section=key, sec_flow=None, sec_target=None, sec_section=None
        )
        await message.answer(
            f"Раздел: {label}\n\n"
            "Пришлите файл .docx — всё, что в нём найдётся, попадёт в этот раздел.\n"
            "Можно прислать несколько файлов подряд."
        )
        return

    if target:
        await asyncio.to_thread(
            teacher_content.set_file_section, telegram_id, subject, target, key
        )

    if flow == FLOW_SORT:
        await message.answer(f"«{target}» → {label}")
        if not await _next_unsorted(message, state, subject):
            await message.answer(
                "Готово, все файлы разложены.",
                reply_markup=uploaded_kb(),
            )
        return

    # Загрузка: запоминаем выбор на остаток сессии
    await state.set_state(None)
    await state.update_data(section=key, sec_flow=None, sec_target=None)

    tail = (
        "Следующие файлы буду класть туда же."
        if key
        else f"Ученик найдёт их в тренировке по разделам, в «{UNSORTED_LABEL}»."
    )
    await message.answer(
        f"Готово: «{target}» → {label}\n\n{tail}",
        reply_markup=uploaded_kb(label, teacher_content.file_token(target)),
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
        "Жду файл .docx с билетами.\n"
        "Чтобы выйти — нажмите любую кнопку меню или отправьте /menu.",
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
