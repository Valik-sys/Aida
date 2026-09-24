"""Экран «🗂 Разделы»: программа преподавателя.

Одно место, где с разделами и темами делается всё: посмотреть, что где
лежит, скрыть лишнее из стандартной программы, завести, переименовать
и удалить своё. Отдельной «Программы» рядом с «Разделами» нет намеренно:
два экрана про одно и то же путают (решено 24.09.2026).

Правила, ради которых экран устроен так, а не проще:

- стандартное скрывается, а не удаляется: к нему привязаны общие вопросы
  и теория, и сетка одна на всех. Своё удаляется по-настоящему;
- переделать стандартную тему = скрыть её и завести свою рядом.
  Переименование стандартной оставило бы под новым названием общие
  вопросы со старым смыслом;
- место с файлами преподавателя ни скрыть, ни удалить молча нельзя —
  сначала бот спрашивает, куда переложить файлы. Иначе вопросы пропали бы
  у учеников без следа;
- «скрыть все» пропускает места с файлами и честно говорит, какие.

Экраны собираются чистыми функциями (`root_screen`, `section_screen`…),
хендлеры только показывают результат — так их можно проверить тестами.
"""

from __future__ import annotations

import asyncio
import logging
from typing import List, Optional, Tuple

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import subjects as subjects_cfg
from bot.keyboards.inline import ALL_MENU_BUTTONS, questions_word, with_count
from bot.states.states import SectionsEdit
from database.db import get_user
from database.models import ROLE_TEACHER
from services import sections as sections_lib, teacher_content


logger = logging.getLogger(__name__)

router = Router()

PREFIX = "prg"
ALL_KEY = "-"          # «все разделы» в кнопках «скрыть/вернуть все»
NONE_TARGET = "none"   # «Смешанные вопросы» как место, куда переложить файлы

ACTION_HIDE = "h"
ACTION_DELETE = "d"

Screen = Tuple[str, InlineKeyboardMarkup]


def _btn(text: str, data: str) -> List[InlineKeyboardButton]:
    return [InlineKeyboardButton(text=text, callback_data=f"{PREFIX}:{data}")]


def _back(data: str, text: str) -> List[InlineKeyboardButton]:
    return [InlineKeyboardButton(text=text, callback_data=data)]


# ---------- Что где лежит ----------

def _custom(tg_id: int, subject: str):
    return (
        teacher_content.custom_sections(tg_id, subject),
        teacher_content.custom_topics(tg_id, subject),
    )


def _place_name(tg_id: int, subject: str, key: str) -> str:
    """Название места для текста: раздел или тема, без стрелок."""
    sections, topics = _custom(tg_id, subject)
    if sections_lib.is_topic(key):
        return sections_lib.topic_title(subject, key, topics)
    return sections_lib.title_of(subject, key, sections)


def _section_heading(tg_id: int, subject: str, key: str) -> str:
    full = next(
        (s.full for s in sections_lib.base_sections(subject) if s.key == key and s.full), ""
    )
    return full or _place_name(tg_id, subject, key)


# ---------- Экраны ----------

def root_screen(tg_id: int, subject: str, note: str = "") -> Screen:
    """Все разделы: стандартные, скрытые и свои — с числом вопросов."""
    sections, topics = _custom(tg_id, subject)
    hidden = teacher_content.hidden_places(tg_id, subject)

    rows: List[List[InlineKeyboardButton]] = []
    any_visible_base = any_hidden_base = False
    for section in sections_lib.merged(subject, sections):
        own = sections_lib.is_custom(section.key)
        count = teacher_content.questions_in_place(tg_id, subject, section.key)
        if own:
            label = with_count(f"{section.title} (ваш)", count)
        elif section.key in hidden:
            any_hidden_base = True
            label = f"🚫 {section.label} · скрыт"
        else:
            any_visible_base = True
            label = with_count(section.label, count)
            hidden_topics = sum(
                1 for t in sections_lib.base_topics(subject, section.key) if t.key in hidden
            )
            if hidden_topics:
                label += f" · скрыто тем: {hidden_topics}"
        rows.append(_btn(label, f"s:{section.key}"))

    rows.append(_btn("➕ Свой раздел", f"new:{ALL_KEY}"))
    if any_visible_base:
        rows.append(_btn("Скрыть все стандартные разделы", f"hideall:{ALL_KEY}"))
    if any_hidden_base:
        rows.append(_btn("Вернуть все стандартные разделы", f"showall:{ALL_KEY}"))
    if teacher_content.files_without_section(tg_id, subject):
        rows.append(_back("trainer:sort", "🗂 Разложить файлы по разделам"))
    rows.append(_back("trainer:root", "◀️ Назад"))

    lines = []
    if note:
        lines += [note, ""]
    lines += [
        "🗂 Разделы и темы",
        "",
        "Нажмите на раздел, чтобы открыть его темы.",
        "Скрытые разделы и темы ученики не видят.",
    ]
    unsorted = teacher_content.counts_by_section(tg_id, subject).get("", 0)
    if unsorted:
        lines.append(f"\n{sections_lib.UNSORTED_TITLE} — {unsorted}")
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def section_screen(tg_id: int, subject: str, key: str, note: str = "") -> Screen:
    """Темы раздела: скрыть или вернуть стандартные, завести свои."""
    sections, topics = _custom(tg_id, subject)
    hidden = teacher_content.hidden_places(tg_id, subject)
    own_section = sections_lib.is_custom(key)
    count = teacher_content.questions_in_place(tg_id, subject, key)

    lines = []
    if note:
        lines += [note, ""]
    lines.append(f"📂 {_section_heading(tg_id, subject, key)} — {count} {questions_word(count)}")

    rows: List[List[InlineKeyboardButton]] = []

    if not own_section and key in hidden:
        lines += ["", "🚫 Раздел скрыт — ученики его не видят."]
        rows.append(_btn("Вернуть раздел", f"show:{key}"))
        rows.append(_btn("◀️ К разделам", "root"))
        return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)

    any_visible = any_hidden = False
    for topic in sections_lib.merged_topics(subject, key, topics):
        n = teacher_content.questions_in_place(tg_id, subject, topic.key)
        title = sections_lib.button_label(topic.title)
        if sections_lib.is_custom(topic.key):
            label = with_count(f"✏️ {title} (ваша)", n)
        elif topic.key in hidden:
            any_hidden = True
            label = f"🚫 {title} · скрыта"
        else:
            any_visible = True
            label = with_count(f"✅ {title}", n)
        rows.append(_btn(label, f"t:{topic.key}"))

    if any_visible or any_hidden:
        lines += ["", "Нажмите на тему, чтобы скрыть или вернуть её."]

    rows.append(_btn("➕ Своя тема", f"new:{key}"))
    if any_visible:
        rows.append(_btn("Скрыть все стандартные темы", f"hideall:{key}"))
    if any_hidden:
        rows.append(_btn("Вернуть все стандартные темы", f"showall:{key}"))
    if own_section:
        rows.append(_btn("✏️ Переименовать раздел", f"ren:{key}"))
        rows.append(_btn("🗑 Удалить раздел", f"del:{key}"))
    else:
        rows.append(_btn("🚫 Скрыть весь раздел", f"hide:{key}"))
    rows.append(_btn("◀️ К разделам", "root"))
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def own_topic_screen(tg_id: int, subject: str, key: str) -> Screen:
    """Своя тема: переименовать или удалить."""
    n = teacher_content.questions_in_place(tg_id, subject, key)
    text = f"✏️ {_place_name(tg_id, subject, key)} — {n} {questions_word(n)}"
    rows = [
        _btn("✏️ Переименовать", f"ren:{key}"),
        _btn("🗑 Удалить", f"del:{key}"),
        _btn("◀️ К разделу", f"s:{sections_lib.section_of(key)}"),
    ]
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def _move_targets(tg_id: int, subject: str, key: str) -> List[Tuple[str, str]]:
    """Куда можно переложить файлы места: (ключ, подпись)."""
    sections, topics = _custom(tg_id, subject)
    hidden = teacher_content.hidden_places(tg_id, subject)
    out: List[Tuple[str, str]] = []

    if sections_lib.is_topic(key):
        parent = sections_lib.section_of(key)
        out.append((parent, f"📚 Весь раздел «{_place_name(tg_id, subject, parent)}»"))
        for topic in sections_lib.merged_topics(subject, parent, topics):
            if topic.key != key and not sections_lib.is_hidden(topic.key, hidden):
                out.append((topic.key, sections_lib.button_label(topic.title)))
    else:
        for section in sections_lib.merged(subject, sections):
            if section.key != key and not sections_lib.is_hidden(section.key, hidden):
                out.append((section.key, section.label))

    out.append((NONE_TARGET, sections_lib.UNSORTED_TITLE))
    return out


def move_screen(tg_id: int, subject: str, action: str, key: str) -> Screen:
    """В месте лежат файлы — спросить, куда их переложить, прежде чем убрать."""
    files = teacher_content.files_in_place(tg_id, subject, key)
    n = teacher_content.questions_in_place(tg_id, subject, key)
    word = "файл" if len(files) == 1 else "файла" if len(files) < 5 else "файлов"
    verb = "удалить" if action == ACTION_DELETE else "скрыть"
    text = (
        f"В «{_place_name(tg_id, subject, key)}» лежат ваши материалы: "
        f"{len(files)} {word}, {n} {questions_word(n)}.\n\n"
        f"Чтобы {verb} это место, их нужно переложить. Куда?"
    )
    rows = [
        _btn(label, f"mv:{action}:{key}:{target}")
        for target, label in _move_targets(tg_id, subject, key)
    ]
    back = f"s:{sections_lib.section_of(key)}"
    rows.append(_btn("Отмена", back))
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def delete_confirm_screen(tg_id: int, subject: str, key: str) -> Screen:
    name = _place_name(tg_id, subject, key)
    what = "тему" if sections_lib.is_topic(key) else "раздел"
    extra = ""
    if not sections_lib.is_topic(key):
        _sections, topics = _custom(tg_id, subject)
        own_topics = [k for k in topics if sections_lib.section_of(k) == key]
        if own_topics:
            extra = f"\nВместе с ним удалятся и его темы: {len(own_topics)}."
    back = f"s:{key}" if not sections_lib.is_topic(key) else f"t:{key}"
    rows = [
        _btn("🗑 Да, удалить", f"delok:{key}"),
        _btn("Отмена", back),
    ]
    return f"Удалить {what} «{name}»?{extra}", InlineKeyboardMarkup(inline_keyboard=rows)


# ---------- Действия ----------

def hide_all(tg_id: int, subject: str, section: Optional[str]) -> str:
    """Скрывает всю стандартную сетку (раздела или целиком), кроме мест с файлами.

    Места с файлами пропускаются, а не перекладываются сами: куда их деть,
    решает преподаватель, и спросить о каждом разом — простыня вопросов.
    """
    if section:
        keys = [t.key for t in sections_lib.base_topics(subject, section)]
    else:
        keys = [s.key for s in sections_lib.base_sections(subject)]

    free = [k for k in keys if not teacher_content.files_in_place(tg_id, subject, k)]
    busy = [k for k in keys if k not in free]
    teacher_content.set_hidden(tg_id, subject, free, True)

    what = "тем" if section else "разделов"
    note = f"Скрыто {what}: {len(free)}."
    if busy:
        names = ", ".join(f"«{_place_name(tg_id, subject, k)}»" for k in busy)
        note += (
            f"\nНе скрыты — там лежат ваши файлы: {names}. "
            "Откройте их, чтобы переложить файлы и скрыть."
        )
    return note


def show_all(tg_id: int, subject: str, section: Optional[str]) -> str:
    if section:
        keys = [t.key for t in sections_lib.base_topics(subject, section)]
    else:
        keys = [s.key for s in sections_lib.base_sections(subject)]
    teacher_content.set_hidden(tg_id, subject, keys, False)
    return "Стандартные темы возвращены." if section else "Стандартные разделы возвращены."


def remove_place(tg_id: int, subject: str, action: str, key: str) -> str:
    """Скрыть стандартное или удалить своё. Файлы к этому моменту переложены."""
    name = _place_name(tg_id, subject, key)
    if action == ACTION_DELETE:
        teacher_content.delete_custom_place(tg_id, subject, key)
        return f"Удалено: «{name}»."
    teacher_content.set_hidden(tg_id, subject, [key], True)
    return f"Скрыто от учеников: «{name}»."


def _after_screen(tg_id: int, subject: str, key: str, note: str) -> Screen:
    """Куда вернуться после действия: к разделу темы или к списку разделов."""
    if sections_lib.is_topic(key):
        return section_screen(tg_id, subject, sections_lib.section_of(key), note)
    if sections_lib.is_custom(key):
        # Свой раздел удалён — его экрана больше нет
        return root_screen(tg_id, subject, note)
    return section_screen(tg_id, subject, key, note)


# ---------- Хендлеры ----------

async def _teacher_subject(telegram_id: int) -> Optional[str]:
    user = await get_user(telegram_id)
    if not user or user.role != ROLE_TEACHER:
        return None
    subject = user.current_subject or subjects_cfg.DEFAULT_SUBJECT
    return subject if subjects_cfg.has_sections(subject) else None


async def _show(message: Message, screen: Screen) -> None:
    text, markup = screen
    try:
        await message.edit_text(text, reply_markup=markup)
        return
    except Exception as exc:  # noqa: BLE001
        if "not modified" in str(exc).lower():
            return
    await message.answer(text, reply_markup=markup)


def _known(tg_id: int, subject: str, key: str) -> bool:
    sections, topics = _custom(tg_id, subject)
    return sections_lib.is_valid(subject, key, sections, topics) and bool(key)


@router.callback_query(lambda c: c.data == "trainer:sections")
async def open_sections(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    subject = await _teacher_subject(callback.from_user.id)
    if not subject:
        return
    await state.set_state(None)
    await _show(callback.message, root_screen(callback.from_user.id, subject))


@router.callback_query(lambda c: (c.data or "").startswith(f"{PREFIX}:"))
async def on_action(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    tg_id = callback.from_user.id
    subject = await _teacher_subject(tg_id)
    if not subject:
        return

    parts = (callback.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""
    key = parts[2] if len(parts) > 2 else ""
    message = callback.message

    if action == "root":
        await state.set_state(None)
        await _show(message, root_screen(tg_id, subject))
        return

    if action in ("hideall", "showall"):
        section = None if key == ALL_KEY else key
        if section and not _known(tg_id, subject, section):
            await _show(message, root_screen(tg_id, subject))
            return
        fn = hide_all if action == "hideall" else show_all
        note = await asyncio.to_thread(fn, tg_id, subject, section)
        screen = section_screen(tg_id, subject, section, note) if section else root_screen(tg_id, subject, note)
        await _show(message, screen)
        return

    if action == "new":
        section = "" if key == ALL_KEY else key
        if section and not _known(tg_id, subject, section):
            await _show(message, root_screen(tg_id, subject))
            return
        await state.set_state(SectionsEdit.waiting_title)
        await state.update_data(prg_mode="new", prg_key=section)
        prompt = (
            "Как назвать новую тему? Напишите название одним сообщением."
            if section else
            "Как назвать новый раздел? Напишите название одним сообщением."
        )
        await _show(message, (prompt, InlineKeyboardMarkup(inline_keyboard=[
            _btn("Отмена", f"s:{section}" if section else "root"),
        ])))
        return

    if action == "mv":
        # prg:mv:{h|d}:{key}:{target}
        if len(parts) < 5:
            return
        kind, key, target = parts[2], parts[3], parts[4]
        if kind not in (ACTION_HIDE, ACTION_DELETE) or not _known(tg_id, subject, key):
            return
        target_key = "" if target == NONE_TARGET else target
        valid_targets = {k for k, _ in _move_targets(tg_id, subject, key)}
        if target not in valid_targets:
            await _show(message, move_screen(tg_id, subject, kind, key))
            return
        moved = await asyncio.to_thread(
            teacher_content.move_place_files, tg_id, subject, key, target_key
        )
        target_name = (
            sections_lib.UNSORTED_TITLE if not target_key
            else _place_name(tg_id, subject, target_key)
        )
        note = remove_place(tg_id, subject, kind, key)
        word = "файл" if moved == 1 else "файла" if moved < 5 else "файлов"
        note = f"Переложено {moved} {word} в «{target_name}».\n{note}"
        await _show(message, _after_screen(tg_id, subject, key, note))
        return

    # Всё ниже работает с конкретным местом, и старая кнопка из истории
    # чата может указывать на уже удалённое — такое не проводим
    if not _known(tg_id, subject, key):
        await _show(message, root_screen(tg_id, subject, "Этого раздела или темы уже нет."))
        return

    if action == "s":
        await state.set_state(None)
        await _show(message, section_screen(tg_id, subject, key))
        return

    if action == "t":
        if sections_lib.is_custom(key):
            await _show(message, own_topic_screen(tg_id, subject, key))
            return
        hidden = teacher_content.hidden_places(tg_id, subject)
        if key in hidden:
            teacher_content.set_hidden(tg_id, subject, [key], False)
            note = f"Снова видно ученикам: «{_place_name(tg_id, subject, key)}»."
            await _show(message, section_screen(tg_id, subject, sections_lib.section_of(key), note))
            return
        action = "hide"  # нажатие на видимую стандартную тему — скрыть

    if action == "show":
        teacher_content.set_hidden(tg_id, subject, [key], False)
        note = f"Снова видно ученикам: «{_place_name(tg_id, subject, key)}»."
        await _show(message, _after_screen(tg_id, subject, key, note))
        return

    if action == "hide":
        if sections_lib.is_custom(key):
            return
        if teacher_content.files_in_place(tg_id, subject, key):
            await _show(message, move_screen(tg_id, subject, ACTION_HIDE, key))
            return
        note = remove_place(tg_id, subject, ACTION_HIDE, key)
        await _show(message, _after_screen(tg_id, subject, key, note))
        return

    if action == "ren":
        if not sections_lib.is_custom(key):
            return
        await state.set_state(SectionsEdit.waiting_title)
        await state.update_data(prg_mode="ren", prg_key=key)
        back = f"t:{key}" if sections_lib.is_topic(key) else f"s:{key}"
        await _show(message, (
            f"Новое название для «{_place_name(tg_id, subject, key)}»? "
            "Напишите его одним сообщением.",
            InlineKeyboardMarkup(inline_keyboard=[_btn("Отмена", back)]),
        ))
        return

    if action == "del":
        if not sections_lib.is_custom(key):
            return
        if teacher_content.files_in_place(tg_id, subject, key):
            await _show(message, move_screen(tg_id, subject, ACTION_DELETE, key))
            return
        await _show(message, delete_confirm_screen(tg_id, subject, key))
        return

    if action == "delok":
        if not sections_lib.is_custom(key):
            return
        # Файл мог появиться, пока висело подтверждение
        if teacher_content.files_in_place(tg_id, subject, key):
            await _show(message, move_screen(tg_id, subject, ACTION_DELETE, key))
            return
        note = remove_place(tg_id, subject, ACTION_DELETE, key)
        await _show(message, _after_screen(tg_id, subject, key, note))
        return


@router.message(
    SectionsEdit.waiting_title,
    ~F.text.in_(ALL_MENU_BUTTONS),
    ~F.text.startswith("/"),
)
async def title_entered(message: Message, state: FSMContext) -> None:
    tg_id = message.from_user.id
    subject = await _teacher_subject(tg_id)
    if not subject:
        await state.set_state(None)
        return

    data = await state.get_data()
    mode = data.get("prg_mode")
    key = data.get("prg_key") or ""
    title = message.text or ""

    if not sections_lib.clean_title(title):
        await message.answer("Название не может быть пустым. Напишите его текстом.")
        return

    await state.set_state(None)

    if mode == "ren":
        ok = await asyncio.to_thread(teacher_content.rename_custom_place, tg_id, subject, key, title)
        note = "Название изменено." if ok else "Не получилось переименовать — этого места уже нет."
        if sections_lib.is_topic(key):
            screen = section_screen(tg_id, subject, sections_lib.section_of(key), note)
        elif ok:
            screen = section_screen(tg_id, subject, key, note)
        else:
            screen = root_screen(tg_id, subject, note)
        text, markup = screen
        await message.answer(text, reply_markup=markup)
        return

    if key:
        new_key = await asyncio.to_thread(teacher_content.add_custom_topic, tg_id, subject, key, title)
        note = f"Тема «{_place_name(tg_id, subject, new_key)}» добавлена." if new_key else ""
        text, markup = section_screen(tg_id, subject, key, note)
    else:
        new_key = await asyncio.to_thread(teacher_content.add_custom_section, tg_id, subject, title)
        note = f"Раздел «{_place_name(tg_id, subject, new_key)}» добавлен." if new_key else ""
        text, markup = root_screen(tg_id, subject, note)
    await message.answer(text, reply_markup=markup)
