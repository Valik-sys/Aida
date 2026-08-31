import datetime as dt
import html
import logging
from typing import List, Optional, Sequence

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message

import config
import subjects as subjects_cfg
from bot.keyboards.inline import (
    MENU_BTN_ASK,
    MENU_BTN_BACK_TO_CABINET,
    MENU_BTN_FLASHCARDS,
    MENU_BTN_HOME,
    MENU_BTN_MY_TRAINER,
    MENU_BTN_SETTINGS,
    MENU_BTN_STUDENTS,
    MENU_BTN_STUDENT_VIEW,
    MENU_BTN_TESTS,
    MENU_BTN_TOPICS,
    MENU_BTN_MISTAKES,
    MENU_BTN_PROGRESS,
    MENU_BTN_UPLOAD,
    REPORT_CARD_PREFIX,
    back_to_mode_kb,
    journal_kb,
    back_to_trainer_kb,
    flashcards_root_kb,
    main_menu_kb,
    mode_help_kb,
    questions_word,
    report_card_kb,
    settings_kb,
    short_text,
    source_choice_kb,
    student_card_kb,
    unbind_confirm_kb,
    student_view_kb,
    students_kb,
    teacher_cabinet_kb,
    tests_root_kb,
    trainer_kb,
    trainer_sections_kb,
    topics_entry_kb,
    mistakes_kb,
)
from bot.handlers.start import invite_link
from bot.states.states import ChatFlow
from database.db import (
    ANSWER_LOG_KEEP_DAYS,
    clear_question_reports,
    get_answer_log,
    count_due,
    count_mistakes,
    ensure_teacher,
    get_accuracy_window,
    get_activity_streak,
    get_hard_questions,
    get_question_reports,
    get_student_progress,
    get_students_stats,
    get_teacher_students,
    get_teacher_totals,
    get_user,
    question_hash,
    unbind_student,
    set_question_status,
    set_teacher_setting,
)
from database.models import ROLE_TEACHER
from rag.indexer import build_vectorstore
from rag.retriever import reset_retriever_cache
from services import (
    content_provider,
    journal,
    reports as reports_lib,
    sections as sections_lib,
    storage,
    teacher_content,
)
from services.sheets import sheets_cache
from services.theory import load_theory


logger = logging.getLogger(__name__)

router = Router()
update_router = Router()


async def _user_context(telegram_id: int) -> tuple[str, str | None, object]:
    """Предмет и роль пользователя. Для незарегистрированных — значения по умолчанию."""
    user = await get_user(telegram_id)
    subject = (user.current_subject if user else None) or subjects_cfg.DEFAULT_SUBJECT
    role = user.role if user else None
    return subject, role, user


async def _open_menu(message: Message) -> None:
    subject, role, _ = await _user_context(message.from_user.id)
    await message.answer(
        f"Главное меню — {subjects_cfg.subject_name(subject)}:",
        reply_markup=main_menu_kb(subject, role),
    )


async def _guard_mode(message: Message, mode: str) -> bool:
    """Пускает в режим, только если он включён у предмета пользователя.

    Кнопки скрытых режимов в меню не появляются, но текст можно ввести руками
    или нажать старую кнопку из истории чата.
    """
    subject, _, _ = await _user_context(message.from_user.id)
    if subjects_cfg.is_mode_enabled(subject, mode):
        return True
    if subjects_cfg.is_mode_available(subject, mode):
        await message.answer("Этот режим временно отключён.")
    else:
        await message.answer(
            f"Для предмета «{subjects_cfg.subject_name(subject)}» этот режим недоступен."
        )
    return False


# ---------- Главное меню ----------

@router.message(Command("menu"))
async def menu_cmd(message: Message, state: FSMContext) -> None:
    await state.clear()
    await _open_menu(message)


@router.message(F.text == MENU_BTN_HOME)
async def menu_home_btn(message: Message, state: FSMContext) -> None:
    await state.clear()
    await _open_menu(message)


# Inline-кнопка «Меню» (из подменю)
@router.callback_query(lambda c: c.data == "menu:back_to_main")
async def menu_back(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    subject, _role, _ = await _user_context(callback.from_user.id)
    # Клавиатуру не пересылаем: у преподавателя может быть открыт режим
    # ученика, и подмена сбросила бы его обратно в кабинет
    await callback.message.answer(f"Главное меню — {subjects_cfg.subject_name(subject)}:")
    await callback.answer()


# ---------- Настройки преподавателя ----------

SETTINGS_ROOT_TEXT = "⚙️ Настройки\n\nЧто настроить:"


async def _source_screen(teacher_id: int, subject: str) -> tuple[str, object]:
    # Спрашиваем ровно у того, кто решает это для тренажёра: свой расчёт
    # разъезжался с настоящим — экран мог показывать источник, которого
    # у предмета нет, и тогда ни одна кнопка не была отмечена галочкой
    user = await get_user(teacher_id)
    if user:
        current = await content_provider.effective_source(user, subject)
    else:
        current = subjects_cfg.allowed_sources(subject)[0]

    # Описываем только выбранный вариант: остальные и так подписаны на кнопках,
    # а три пояснения подряд превращают экран в сплошной текст
    text = (
        "📚 Материалы тренажёра\n\n"
        f"Сейчас: <b>{subjects_cfg.SOURCE_LABELS[current]}</b>\n"
        f"{subjects_cfg.SOURCE_HINTS[current]}"
    )
    return text, source_choice_kb(subject, current)


@router.message(F.text == MENU_BTN_SETTINGS)
async def menu_settings(message: Message, state: FSMContext) -> None:
    await state.clear()
    _subject, role, _ = await _user_context(message.from_user.id)
    if role != ROLE_TEACHER:
        await message.answer("Этот раздел доступен преподавателям.")
        return
    await message.answer(SETTINGS_ROOT_TEXT, reply_markup=settings_kb())


@router.callback_query(lambda c: c.data == "settings:root")
async def settings_root(callback: CallbackQuery) -> None:
    await callback.answer()
    await _replace(callback.message, SETTINGS_ROOT_TEXT, settings_kb())


@router.callback_query(lambda c: c.data == "settings:source")
async def settings_source(callback: CallbackQuery) -> None:
    subject, role, _ = await _user_context(callback.from_user.id)
    await callback.answer()
    if role != ROLE_TEACHER:
        return
    text, markup = await _source_screen(callback.from_user.id, subject)
    await _replace(callback.message, text, markup, parse_mode="HTML")


@router.callback_query(lambda c: c.data and c.data.startswith("settings:source:"))
async def settings_source_set(callback: CallbackQuery) -> None:
    value = (callback.data or "").rsplit(":", 1)[-1]
    subject, role, _ = await _user_context(callback.from_user.id)

    if role != ROLE_TEACHER or value not in subjects_cfg.allowed_sources(subject):
        await callback.answer()
        return

    await set_teacher_setting(
        callback.from_user.id, subject, content_provider.MATERIALS_SOURCE, value
    )
    await callback.answer("Сохранено")
    text, markup = await _source_screen(callback.from_user.id, subject)
    await _replace(callback.message, text, markup, parse_mode="HTML")


# ---------- Пояснения к режимам ----------

# Держим отдельно от экрана входа: длинный текст при каждом заходе мешает,
# а один раз прочитать полезно.
MODE_HELP = {
    "ask": (
        "❓ Как это работает\n\n"
        "Спрашивай что угодно по курсу — объясню своими словами, "
        "без зубрёжки формулировок.\n\n"
        "Отвечаю по материалам курса.\n\n"
        "Например:\n"
        "• чем Люблинская уния отличается от Кревской\n"
        "• почему началось восстание Калиновского\n"
        "• что такое фольварк"
    ),
    "tests": (
        "🎯 Как это работает\n\n"
        "Вопросы из материалов твоего преподавателя. "
        "Часть А — выбираешь номер варианта, часть Б — пишешь ответ.\n\n"
        "После каждого ответа сразу видно, верно или нет."
    ),
    "flashcards": (
        "⚡ Как это работает\n\n"
        "Термин на одной стороне, ответ на другой. "
        "Помечаешь «знаю» или «не знаю» — то, что не знаешь, вернётся ещё раз."
    ),
}


# Режимы, у которых есть свой экран выбора — к нему и возвращаем после пояснения
_MODES_WITH_SUBMENU = {"tests", "flashcards"}

# Заголовки экранов выбора — используются и при входе, и при возврате
MODE_SCREENS = {
    "tests": "🎯 Тренировка\n\nВыбери, как заниматься:",
    "flashcards": "⚡ Карточки\n\nВыбери, что повторяем:",
}


async def _replace(message: Message, text: str, markup=None, parse_mode=None) -> None:
    """Заменяет сообщение на месте вместо отправки нового.

    Навигация по инлайн-меню не должна засорять чат: пояснение и список
    выбора — это один и тот же экран в разных состояниях.
    Если отредактировать нельзя (сообщение слишком старое) — отправляем новое.
    """
    try:
        await message.edit_text(text, reply_markup=markup, parse_mode=parse_mode)
    except Exception:  # noqa: BLE001
        await message.answer(text, reply_markup=markup, parse_mode=parse_mode)


@router.callback_query(lambda c: c.data and c.data.startswith("help:"))
async def mode_help(callback: CallbackQuery) -> None:
    mode = (callback.data or "").split(":", 1)[1]
    text = MODE_HELP.get(mode)
    await callback.answer()
    if not text:
        return

    markup = back_to_mode_kb(mode) if mode in _MODES_WITH_SUBMENU else None
    await _replace(callback.message, text, markup)


@router.callback_query(lambda c: c.data and c.data.startswith("open:"))
async def open_mode_menu(callback: CallbackQuery, state: FSMContext) -> None:
    """Возврат к выбору внутри режима — тем же сообщением."""
    mode = (callback.data or "").split(":", 1)[1]
    await callback.answer()

    if mode == "tests":
        await _replace(callback.message, MODE_SCREENS["tests"], tests_root_kb())
    elif mode == "flashcards":
        await _replace(callback.message, MODE_SCREENS["flashcards"], flashcards_root_kb())


# ---------- Переключение кабинет ↔ режим ученика ----------

@router.message(F.text == MENU_BTN_STUDENT_VIEW)
async def switch_to_student_view(message: Message, state: FSMContext) -> None:
    await state.clear()
    subject, role, _ = await _user_context(message.from_user.id)
    if role != ROLE_TEACHER:
        return
    await message.answer(
        "🎓 Режим ученика\n\n"
        "Дальше вы видите бота так же, как его видят ваши ученики.",
        reply_markup=student_view_kb(subject, for_teacher=True),
    )


@router.message(F.text == MENU_BTN_BACK_TO_CABINET)
async def switch_to_cabinet(message: Message, state: FSMContext) -> None:
    await state.clear()
    subject, role, _ = await _user_context(message.from_user.id)
    if role != ROLE_TEACHER:
        return
    await message.answer(
        f"Кабинет преподавателя — {subjects_cfg.subject_name(subject)}",
        reply_markup=teacher_cabinet_kb(subject),
    )


# ---------- Выбор режима (ReplyKeyboard) ----------

@router.message(F.text == MENU_BTN_TESTS)
async def menu_tests(message: Message, state: FSMContext) -> None:
    await state.clear()
    if not await _guard_mode(message, "tests"):
        return
    # Показываем, что подошёл срок повтора: иначе интервальное повторение
    # работает незаметно и ученик не понимает, зачем возвращаться каждый день
    subject, _role, user = await _user_context(message.from_user.id)
    due = await count_due(
        message.from_user.id,
        teacher_id=content_provider.resolve_teacher_id(user) if user else None,
        subject=subject,
    )
    text = MODE_SCREENS["tests"]
    if due:
        text = f"🎯 Тренировка\n\nК повторению сегодня: {due}\n\nВыбери, как заниматься:"
    await message.answer(text, reply_markup=tests_root_kb())


@router.message(F.text == MENU_BTN_FLASHCARDS)
async def menu_flashcards(message: Message, state: FSMContext) -> None:
    await state.clear()
    if not await _guard_mode(message, "flashcards"):
        return
    await message.answer(MODE_SCREENS["flashcards"], reply_markup=flashcards_root_kb())


@router.message(F.text == MENU_BTN_TOPICS)
async def menu_topics(message: Message, state: FSMContext) -> None:
    await state.clear()
    if not await _guard_mode(message, "topics"):
        return
    await message.answer(
        "📚 По темам\n\n"
        "Выбери раздел — изучи теорию, закрепи карточками и проверь себя тестом.",
        reply_markup=topics_entry_kb(),
    )


@router.message(F.text == MENU_BTN_MISTAKES)
async def menu_mistakes(message: Message, state: FSMContext) -> None:
    await state.clear()
    if not await _guard_mode(message, "mistakes"):
        return
    n = await count_mistakes(message.from_user.id)
    if n == 0:
        text = "🔁 Мои ошибки\n\nЧисто — ошибок не накопилось."
    else:
        text = (
            f"🔁 Мои ошибки\n\nНакоплено вопросов: {n}\n\n"
            "Пройди их заново — верные ответы уйдут из списка."
        )
    await message.answer(text, reply_markup=mistakes_kb())


PROGRESS_BAR_WIDTH = 10


def _bar(done: int, total: int) -> str:
    if total <= 0:
        return ""
    filled = round(PROGRESS_BAR_WIDTH * done / total)
    return "▰" * filled + "▱" * (PROGRESS_BAR_WIDTH - filled)


@router.message(F.text == MENU_BTN_PROGRESS)
async def menu_progress(message: Message, state: FSMContext) -> None:
    await state.clear()
    if not await _guard_mode(message, "progress"):
        return

    user_id = message.from_user.id
    subject, _role, user = await _user_context(user_id)
    bundle = await content_provider.get_tests(user_id)
    total = len(bundle.rows)

    progress = await get_student_progress(user_id)
    if not progress["attempts"]:
        await message.answer(
            "📈 Мой прогресс\n\n"
            "Пока пусто — ответь на первые вопросы, и здесь появится статистика."
        )
        return

    mastered = progress["mastered"]
    lines = ["📈 Мой прогресс", ""]

    # Показываем путь целиком: освоенное из всего, что есть в тренажёре,
    # а не из того, что ученик успел повидать
    if total:
        lines.append(f"Освоено {mastered} из {total}")
        lines.append(f"{_bar(mastered, total)}  {round(100 * mastered / total)}%")
    else:
        lines.append(f"Освоено вопросов: {mastered}")

    streak = await get_activity_streak(user_id)
    if streak:
        day_word = "день" if streak == 1 else "дня" if streak < 5 else "дней"
        lines.append(f"\n🔥 Серия: {streak} {day_word} подряд")

    week = await get_accuracy_window(user_id, 7)
    previous = await get_accuracy_window(user_id, 14, 7)
    if week is not None:
        trend = ""
        if previous is not None:
            if week > previous:
                trend = f" (было {previous}%, стало лучше)"
            elif week < previous:
                trend = f" (на прошлой неделе {previous}%)"
        lines.append(f"Верных ответов за неделю: {week}%{trend}")

    due = await count_due(
        user_id,
        teacher_id=content_provider.resolve_teacher_id(user) if user else None,
        subject=subject,
    )
    if due:
        lines.append(f"\n🔁 К повторению сегодня: {due}")

    await message.answer("\n".join(lines), reply_markup=journal_kb())


# ---------- Журнал файлом ----------

async def _send_journal(
    message: Message,
    user_ids: list[int],
    subject: str,
    teacher_id: int | None,
    with_student: bool,
    prefix: str,
) -> None:
    """Собирает журнал и присылает файлом.

    Тему берём из строк тренажёра того преподавателя, чьи это ученики:
    в записи об ответе лежит только отпечаток вопроса, а место живёт
    в контенте — так тема остаётся верной и у старых ответов.
    """
    entries = await get_answer_log(user_ids, subject=subject)
    if not entries:
        await message.answer(
            "Пока нечего выгружать — ответов ещё не было.\n"
            f"В журнал попадают ответы за последние {ANSWER_LOG_KEEP_DAYS} дней."
        )
        return

    rows = teacher_content.load_tests(teacher_id, subject) if teacher_id else []
    if not rows:
        rows = list(sheets_cache.base_tests_rows)

    places = journal.place_by_question(rows, question_hash)
    names = {u.telegram_id: (u.name or f"Ученик {u.telegram_id}") for u in
             (await get_teacher_students(teacher_id) if with_student and teacher_id else [])}

    entries_rows = journal.build(
        entries,
        subject=subject,
        places=places,
        names=names,
        custom_sections=teacher_content.custom_sections(teacher_id, subject) if teacher_id else None,
        custom_topics=teacher_content.custom_topics(teacher_id, subject) if teacher_id else None,
    )

    payload = journal.to_csv(entries_rows, with_student=with_student)
    await message.answer_document(
        BufferedInputFile(payload, filename=journal.filename(prefix)),
        caption=(
            f"Занятий в журнале: {len(entries_rows)}\n"
            "Открывается в Excel и Google Таблицах."
        ),
    )


@router.callback_query(F.data == "students:journal")
async def students_journal(callback: CallbackQuery) -> None:
    """Журнал по всем ученикам — преподавателю."""
    teacher_id = callback.from_user.id
    subject, role, _user = await _user_context(teacher_id)
    await callback.answer()
    if role != ROLE_TEACHER:
        return

    students = await get_teacher_students(teacher_id)
    if not students:
        await callback.message.answer("Учеников пока нет.")
        return

    await _send_journal(
        callback.message,
        [s.telegram_id for s in students],
        subject,
        teacher_id,
        with_student=True,
        prefix="Журнал класса",
    )


@router.callback_query(F.data == "progress:journal")
async def student_journal(callback: CallbackQuery) -> None:
    """Свой журнал — ученику."""
    user_id = callback.from_user.id
    subject, _role, user = await _user_context(user_id)
    await callback.answer()

    await _send_journal(
        callback.message,
        [user_id],
        subject,
        content_provider.resolve_teacher_id(user) if user else None,
        with_student=False,
        prefix="Мой журнал",
    )


@router.message(F.text == MENU_BTN_ASK)
async def menu_ask(message: Message, state: FSMContext) -> None:
    await state.clear()
    if not await _guard_mode(message, "ask"):
        return
    subject, _, _ = await _user_context(message.from_user.id)
    if subjects_cfg.is_stub(subject):
        await message.answer(
            f"Материалы по предмету «{subjects_cfg.subject_name(subject)}» "
            "ещё не загружены — режим пока не отвечает."
        )
        return
    await state.set_state(ChatFlow.waiting_question)
    await message.answer(
        "❓ Задать вопрос\n\nНапиши, что хочешь узнать по курсу.",
        reply_markup=mode_help_kb("ask"),
    )


# ---------- Меню преподавателя ----------

# Кнопка «Загрузить билеты» обрабатывается в bot/handlers/teacher_upload.py


@router.message(F.text == MENU_BTN_MY_TRAINER)
async def menu_my_trainer(message: Message, state: FSMContext) -> None:
    await state.clear()
    subject, role, _ = await _user_context(message.from_user.id)
    if role != ROLE_TEACHER:
        await message.answer("Этот раздел доступен преподавателям.")
        return

    text, markup = await _trainer_screen(message.from_user.id, subject)
    await message.answer(text, reply_markup=markup)


# ---------- Состояние тренажёра ----------

def _parse_iso(value) -> "dt.datetime | None":
    try:
        return dt.datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


async def _trainer_screen(teacher_id: int, subject: str):
    """Готов ли тренажёр и на что хватает вопросов.

    Имена файлов сюда не выносим: преподавателю важен состав, а не то,
    как он его загружал. Файлы нужны только там, где с ними проблема.
    """
    document = teacher_content.load_tests_document(teacher_id, subject)
    rows = document.get("rows") or []
    files = teacher_content.files_overview(teacher_id, subject)
    failed = [f for f in files if not f["accepted"]]

    def warning() -> str:
        if not failed:
            return ""
        # Причину показываем сразу: чаще всего она одна на все файлы,
        # и тогда преподавателю не нужно никуда заходить
        reasons = {f["problem"] for f in failed}
        word = "файл" if len(failed) == 1 else "файла" if len(failed) < 5 else "файлов"
        if len(reasons) == 1:
            return f"\n⚠ {len(failed)} {word} не подключились: {reasons.pop()}"
        return f"\n⚠ Не подключились файлов: {len(failed)}"

    has_sections = subjects_cfg.has_sections(subject) and bool(rows)
    reported = await get_question_reports(teacher_id, subject)

    if not rows:
        lines = ["👀 Мой тренажёр", "", "Пока пусто — загрузите свои билеты."]
        if failed:
            lines.append(warning())
        return "\n".join(lines), trainer_kb(has_files=bool(files))

    with_options = sum(1 for r in rows if any(r.get(f"Вар.{i}") for i in range(1, 6)))
    text_answers = len(rows) - with_options
    updated = _humanize_date(_parse_iso(document.get("updated_at")))

    lines = [
        "👀 Мой тренажёр",
        "",
        "Готов к занятиям",
        f"{len(rows)} вопросов · обновлён {updated}",
        "",
        f"С вариантами ответа — {with_options}",
        f"С текстовым ответом — {text_answers}",
    ]

    unsorted_count = sum(1 for r in rows if not (r.get("Раздел") or "").strip())
    if has_sections and unsorted_count:
        lines.append(f"\n{sections_lib.UNSORTED_TITLE} — {unsorted_count}")

    if reported:
        lines.append(f"\n⚠️ Спорные вопросы — {len(reported)}")

    if failed:
        lines.append(warning())

    return "\n".join(lines), trainer_kb(
        has_files=bool(files),
        has_sections=has_sections,
        has_reports=bool(reported),
    )


# ---------- Разделы ----------

def _sections_text(teacher_id: int, subject: str) -> tuple[str, bool]:
    """Состав тренажёра по разделам. Второе значение — есть ли что раскладывать.

    Пустые разделы не показываем: список программы длинный, а преподавателю
    здесь важно, что у него уже есть, а не чего нет.
    """
    counts = teacher_content.counts_by_section(teacher_id, subject)
    custom = teacher_content.custom_sections(teacher_id, subject)
    custom_topics = teacher_content.custom_topics(teacher_id, subject)

    lines = ["🗂 Разделы и темы", ""]
    filled = 0
    for section in sections_lib.merged(subject, custom):
        # В разделе лежит и то, что положено в него самого, и то, что
        # разложено по темам внутри: в итоге у раздела показывается сумма,
        # а темы перечисляются под ним.
        topics = [
            (topic, counts.get(topic.key, 0))
            for topic in sections_lib.merged_topics(subject, section.key, custom_topics)
        ]
        topics = [(topic, count) for topic, count in topics if count]
        own = counts.get(section.key, 0)
        total = own + sum(count for _, count in topics)

        if total:
            filled += 1
            lines.append(f"{section.label} — {total}")
            for topic, count in topics:
                lines.append(f"   • {sections_lib.button_label(topic.title)} — {count}")

    unsorted_count = counts.get("", 0)
    if not filled and not unsorted_count:
        return "🗂 Разделы\n\nПока ничего не загружено.", False

    if not filled:
        lines.append("Пока ничего не разложено.")

    pending = teacher_content.files_without_section(teacher_id, subject)
    if unsorted_count:
        lines.append(f"\n{sections_lib.UNSORTED_TITLE} — {unsorted_count}")
        if pending:
            word = "файл" if len(pending) == 1 else "файла" if len(pending) < 5 else "файлов"
            lines.append(
                f"Сюда попали {len(pending)} {word} без раздела — "
                "если это не готовые варианты, разложите их."
            )

    lines.append(
        "\nРаздел и тема выбираются при загрузке файла "
        "и относятся ко всему файлу целиком."
    )
    return "\n".join(lines), bool(pending)


@router.callback_query(lambda c: c.data == "trainer:sections")
async def trainer_sections(callback: CallbackQuery) -> None:
    subject, role, _ = await _user_context(callback.from_user.id)
    await callback.answer()
    if role != ROLE_TEACHER:
        return
    text, has_unsorted = _sections_text(callback.from_user.id, subject)
    await _replace(callback.message, text, trainer_sections_kb(has_unsorted))


# ---------- Спорные вопросы ----------

def _sort_reports(items: List[dict]) -> List[dict]:
    """Сначала скрытые — они уже не работают у учеников, это срочно.

    Потом заявленный брак, в конце «не поняли»: там торопиться некуда,
    вопрос жив и, скорее всего, исправен.
    """
    def weight(item: dict) -> tuple:
        hidden = item.get("status") == reports_lib.STATUS_HIDDEN
        broken = reports_lib.count_broken(item.get("reasons") or [])
        return (0 if hidden else 1, 0 if broken else 1, item.get("created_at") or "")

    return sorted(items, key=weight)


def _find_question(teacher_id: int, subject: str, qhash: str) -> Optional[dict]:
    """Строка вопроса по отпечатку — чтобы показать его целиком, как видит ученик.

    Ищем в обоих слоях: при политике «мои, а пока их нет — общие» ученики
    занимаются по базовому набору, и жалоба приходит именно на такой вопрос.
    Искать только у преподавателя — значит показывать ему пустую карточку.
    """
    for row in teacher_content.load_tests(teacher_id, subject):
        if question_hash(row.get("Вопрос") or "") == qhash:
            return row
    for row in sheets_cache.tests_rows or []:
        if question_hash(row.get("Вопрос") or "") == qhash:
            return row
    return None


def _report_card(item: dict, row: Optional[dict], pos: int, total: int) -> str:
    """Карточка одного спорного вопроса.

    Показываем вопрос целиком с вариантами: без них преподавателю нечем
    судить, а с ними брак виден с одного взгляда — «вариантов нет»
    перестаёт быть чужим утверждением.
    """
    lines = [f"⚠️ Спорные вопросы · {pos} из {total}", ""]

    if row:
        lines.append((row.get("Вопрос") or "").strip())
        options = [
            (row.get(f"Вар.{i}") or "").strip() for i in range(1, 6)
        ]
        options = [o for o in options if o]
        if options:
            lines.append("")
            lines.extend(f"{i}. {text}" for i, text in enumerate(options, 1))
        else:
            lines.append("\n⚠️ Вариантов ответа нет")
        answer = (row.get("Ответ") or "").strip()
        if answer:
            lines.append(f"\nПравильный ответ: {answer}")
    else:
        # Файл могли удалить — тогда от вопроса остался только отпечаток
        lines.append(short_text(item.get("preview") or "", 200))
        lines.append("\nВопроса больше нет в тренажёре.")

    students = item.get("students") or []
    reasons = item.get("reasons") or []
    who = ", ".join(dict.fromkeys(students))
    if len(set(reasons)) == 1:
        what = reports_lib.REASON_SHORT.get(reasons[0], "")
    else:
        what = ", ".join(dict.fromkeys(
            reports_lib.REASON_SHORT.get(r, "") for r in reasons
        ))
    lines.append(f"\n{who} · {what}")

    # Знаменатель меньше трёх ничего не говорит — и лучше промолчать
    answered = item.get("answered_by") or 0
    wrong = item.get("wrong_by") or 0
    if answered >= 3:
        lines.append(f"Ошиблись {wrong} из {answered}")

    if item.get("status") == reports_lib.STATUS_HIDDEN:
        lines.append("🚫 Скрыт у учеников")

    return "\n".join(lines)


async def _reports_screen(teacher_id: int, subject: str, skipped: Sequence[str] = ()):
    """Экран разбора: одна карточка за раз, следующая приходит на место прежней."""
    items = _sort_reports(await get_question_reports(teacher_id, subject))
    pending = [i for i in items if i["question_hash"] not in set(skipped)]

    if not pending:
        text = "⚠️ Спорные вопросы\n\nВсе разобраны."
        return text, back_to_trainer_kb(), None

    item = pending[0]
    row = _find_question(teacher_id, subject, item["question_hash"])
    hidden = item.get("status") == reports_lib.STATUS_HIDDEN
    text = _report_card(item, row, len(items) - len(pending) + 1, len(items))
    return text, report_card_kb(hidden), item["question_hash"]


@router.callback_query(lambda c: c.data == "trainer:reports")
async def trainer_reports(callback: CallbackQuery, state: FSMContext) -> None:
    subject, role, _ = await _user_context(callback.from_user.id)
    await callback.answer()
    if role != ROLE_TEACHER:
        return
    await state.update_data(rep_skipped=[])
    text, markup, qhash = await _reports_screen(callback.from_user.id, subject)
    await state.update_data(rep_current=qhash)
    await _replace(callback.message, text, markup)


@router.callback_query(lambda c: (c.data or "").startswith(f"{REPORT_CARD_PREFIX}:"))
async def report_card_action(callback: CallbackQuery, state: FSMContext) -> None:
    subject, role, _ = await _user_context(callback.from_user.id)
    await callback.answer()
    if role != ROLE_TEACHER:
        return

    action = (callback.data or "").split(":", 1)[1]
    data = await state.get_data()
    qhash = data.get("rep_current")
    skipped = list(data.get("rep_skipped") or [])
    teacher_id = callback.from_user.id

    if qhash:
        if action == "ok":
            # Подтверждён преподавателем — автоматически больше не скроется,
            # иначе следующие двое учеников уберут его снова
            await set_question_status(
                teacher_id, subject, qhash, reports_lib.STATUS_CONFIRMED
            )
            await clear_question_reports(teacher_id, qhash)
        elif action == "remove":
            await set_question_status(
                teacher_id, subject, qhash, reports_lib.STATUS_REMOVED
            )
            await clear_question_reports(teacher_id, qhash)
        elif action == "later":
            skipped.append(qhash)
            await state.update_data(rep_skipped=skipped)

    text, markup, next_hash = await _reports_screen(teacher_id, subject, skipped)
    await state.update_data(rep_current=next_hash)
    await _replace(callback.message, text, markup)


# Сколько файлов показывать списком, остальные — числом
FILES_SHOWN = 8


def _files_text(teacher_id: int, subject: str) -> str:
    files = teacher_content.files_overview(teacher_id, subject)
    if not files:
        return "📎 Загруженные файлы\n\nПока ничего не загружено."

    assigned = teacher_content.file_sections(teacher_id, subject)
    custom = teacher_content.custom_sections(teacher_id, subject)

    lines = ["📎 Загруженные файлы", ""]
    for item in files[:FILES_SHOWN]:
        accepted = item["accepted"]
        if accepted:
            section = assigned.get(str(item["filename"]), "")
            where = f" · {sections_lib.title_of(subject, section, custom)}" if section else ""
            lines.append(
                f"✓ {item['filename']} — {accepted} {questions_word(accepted)}{where}"
            )
        else:
            lines.append(f"✗ {item['filename']} — {item['problem']}")

    hidden = len(files) - FILES_SHOWN
    if hidden > 0:
        lines.append(f"   …и ещё {hidden}")

    # Снимаем главный страх: боятся наплодить дублей и потому не перезаливают
    lines.append(
        "\nПовторная загрузка файла заменяет его вопросы — дубликатов не будет."
    )
    return "\n".join(lines)


@router.callback_query(lambda c: c.data == "trainer:files")
async def trainer_files(callback: CallbackQuery) -> None:
    subject, role, _ = await _user_context(callback.from_user.id)
    await callback.answer()
    if role != ROLE_TEACHER:
        return
    await _replace(
        callback.message,
        _files_text(callback.from_user.id, subject),
        back_to_trainer_kb(),
    )


@router.callback_query(lambda c: c.data == "trainer:root")
async def trainer_root(callback: CallbackQuery) -> None:
    subject, role, _ = await _user_context(callback.from_user.id)
    await callback.answer()
    if role != ROLE_TEACHER:
        return
    text, markup = await _trainer_screen(callback.from_user.id, subject)
    await _replace(callback.message, text, markup)


@router.message(F.text == MENU_BTN_STUDENTS)
async def menu_students(message: Message, state: FSMContext) -> None:
    await state.clear()
    telegram_id = message.from_user.id
    subject, role, _ = await _user_context(telegram_id)
    if role != ROLE_TEACHER:
        await message.answer("Этот раздел доступен преподавателям.")
        return

    text, markup = await _students_screen(message.bot, telegram_id, subject)
    await message.answer(text, reply_markup=markup, parse_mode="HTML")


# ---------- Ученики преподавателя ----------

def _utcnow() -> dt.datetime:
    # Наивный UTC — в том же виде, в каком время лежит в БД
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def _humanize_date(value) -> str:
    """«сегодня» / «вчера» / «5 дней назад» / дата — как удобнее читать."""
    if not value:
        return "—"
    days = (_utcnow() - value).days
    if days <= 0:
        return "сегодня"
    if days == 1:
        return "вчера"
    if days < 7:
        return f"{days} дн. назад"
    return value.strftime("%d.%m.%Y")


def _is_active(user, days: int = 7) -> bool:
    if not user.last_active_at:
        return False
    return (_utcnow() - user.last_active_at).days < days


# Отстающим считаем того, кто занимался достаточно, но заметно хуже класса.
# Порог по числу ответов нужен, чтобы две неудачные попытки не записывали
# ученика в отстающие.
LAGGING_MIN_ATTEMPTS = 10
LAGGING_GAP = 15


async def _lagging_students(
    teacher_id: int, subject: str, class_accuracy: int, limit: int = 3
) -> list:
    """Ученики, которые занимаются, но заметно отстают по точности.

    Именно «отстают по существу», а не «мало занимались»: второе видно
    и так по датам, а первое — только по цифрам.
    """
    if not class_accuracy:
        return []

    students = await get_students_stats(teacher_id, subject)
    lagging = [
        s for s in students
        if s["attempts"] >= LAGGING_MIN_ATTEMPTS
        and s["accuracy"] is not None
        and s["accuracy"] <= class_accuracy - LAGGING_GAP
    ]
    lagging.sort(key=lambda s: s["accuracy"])
    return lagging[:limit]


PREVIEW_LIMIT = 55


def _one_line(text: str, limit: int = PREVIEW_LIMIT) -> str:
    """Однострочный кусочек вопроса, пригодный для HTML.

    В тексте вопроса бывают переносы строк и пункты «А)» — при вставке
    как есть список разъезжается. И экранирование обязательно: сообщение
    уходит с разметкой, а в вопросах встречаются угловые скобки.
    """
    flat = " ".join((text or "").split())
    if len(flat) > limit:
        flat = flat[:limit].rstrip(" ,.;:") + "…"
    return html.escape(flat)


async def _students_screen(bot, teacher_id: int, subject: str, page: int = 0):
    """Общая сводка по ученикам плюс страница со списком."""
    teacher = await ensure_teacher(teacher_id, subject)
    students = await get_teacher_students(teacher_id)
    link = await invite_link(bot, teacher.invite_code)

    blocks: list[str] = ["👥 <b>Мои ученики</b>"]

    if not students:
        blocks.append(
            "Пока никто не присоединился.\nРаздайте ссылку — ученики появятся здесь."
        )
    else:
        active = sum(1 for s in students if _is_active(s))
        head = [f"Всего: {len(students)} · занимались на неделе: {active}"]

        totals = await get_teacher_totals(teacher_id, subject=subject)
        if totals["attempts"]:
            head.append(f"Ответов: {totals['attempts']} · верных {totals['accuracy']}%")
        else:
            head.append("Ответов пока нет.")
        blocks.append("\n".join(head))

        lagging = await _lagging_students(teacher_id, subject, totals["accuracy"])
        if lagging:
            names = "\n".join(
                f"• {html.escape(s['name'] or 'без имени')} — {s['accuracy']}%"
                for s in lagging
            )
            blocks.append(
                f"<b>Отстают</b>  <i>при среднем {totals['accuracy']}%</i>\n{names}"
            )

        hard = await get_hard_questions(teacher_id, limit=3, subject=subject)
        if hard:
            items = "\n".join(
                f"• {_one_line(item['preview'])}\n  <i>ошибок {item['errors']} из "
                f"{item['attempts']}</i>"
                for item in hard
            )
            blocks.append(f"<b>Чаще всего ошибаются</b>\n{items}")

        blocks.append("<i>Нажмите на ученика, чтобы посмотреть подробности.</i>")

    blocks.append(f"<b>Приглашение</b>\n{link}\nкод <code>{teacher.invite_code}</code>")

    return "\n\n".join(blocks), students_kb(students, page)


async def _show_students(callback: CallbackQuery, page: int) -> None:
    subject, role, _ = await _user_context(callback.from_user.id)
    if role != ROLE_TEACHER:
        return
    text, markup = await _students_screen(
        callback.bot, callback.from_user.id, subject, page
    )
    await _replace(callback.message, text, markup, parse_mode="HTML")


@router.callback_query(lambda c: c.data == "noop")
async def noop(callback: CallbackQuery) -> None:
    """Счётчик страниц — не кнопка, нажатие просто гасим."""
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("students:page:"))
async def students_page(callback: CallbackQuery) -> None:
    await callback.answer()
    try:
        page = int((callback.data or "").rsplit(":", 1)[-1])
    except ValueError:
        page = 0
    await _show_students(callback, page)


@router.callback_query(lambda c: c.data and c.data.startswith("student:"))
async def student_card(callback: CallbackQuery) -> None:
    """Подробности по одному ученику."""
    parts = (callback.data or "").split(":")
    try:
        student_id = int(parts[1])
        page = int(parts[2]) if len(parts) > 2 else 0
    except (IndexError, ValueError):
        await callback.answer()
        return

    teacher_id = callback.from_user.id
    student = await get_user(student_id)
    await callback.answer()

    # Смотреть можно только своих учеников
    if not student or student.teacher_id != teacher_id:
        await _replace(callback.message, "Этот ученик больше не привязан к вам.")
        return

    mistakes = await count_mistakes(student_id)
    progress = await get_student_progress(student_id)

    lines = [
        f"👤 <b>{html.escape(student.name or 'Без имени')}</b>",
        "",
        f"Присоединился: {_humanize_date(student.created_at)}",
        # Время последнего ответа точнее, чем последней активности:
        # заходил в бот — не то же самое, что занимался
        f"Последнее занятие: "
        f"{_humanize_date(progress['last_answered_at'] or student.last_active_at)}",
    ]

    if progress["attempts"]:
        lines += [
            "",
            f"Отвечено: {progress['attempts']} раз на {progress['questions']} вопросов",
            f"Верных ответов: {progress['accuracy']}%",
            f"Освоено вопросов: {progress['mastered']} из {progress['questions']}",
        ]
    else:
        lines.append("\nПока не отвечал ни на один вопрос.")

    if mistakes:
        lines.append(f"Ошибок к повторению: {mistakes}")

    await _replace(
        callback.message,
        "\n".join(lines),
        student_card_kb(page, student_id),
        parse_mode="HTML",
    )


# ---------- Отвязка ученика ----------

def _unbind_parts(data: str) -> tuple[int, int] | None:
    parts = (data or "").split(":")
    try:
        return int(parts[2]), int(parts[3]) if len(parts) > 3 else 0
    except (IndexError, ValueError):
        return None


@router.callback_query(lambda c: c.data and c.data.startswith("unbind:ask:"))
async def unbind_ask(callback: CallbackQuery) -> None:
    """Спрашиваем подтверждение и честно говорим, что будет."""
    parsed = _unbind_parts(callback.data or "")
    await callback.answer()
    if not parsed:
        return
    student_id, page = parsed

    student = await get_user(student_id)
    if not student or student.teacher_id != callback.from_user.id:
        await _replace(callback.message, "Этот ученик больше не привязан к вам.")
        return

    name = html.escape(student.name or f"Ученик {student_id}")
    await _replace(
        callback.message,
        f"Отвязать <b>{name}</b>?\n\n"
        "Он потеряет доступ к вашим материалам и пропадёт из списка "
        "и из статистики класса.\n"
        "Его собственный прогресс сохранится, но вернуть его можно будет "
        "только новым приглашением.",
        unbind_confirm_kb(student_id, page),
        parse_mode="HTML",
    )


@router.callback_query(lambda c: c.data and c.data.startswith("unbind:do:"))
async def unbind_do(callback: CallbackQuery) -> None:
    parsed = _unbind_parts(callback.data or "")
    await callback.answer()
    if not parsed:
        return
    student_id, page = parsed

    student = await get_user(student_id)
    name = html.escape(student.name or f"Ученик {student_id}") if student else ""

    if not await unbind_student(student_id, callback.from_user.id):
        # Либо уже отвязан, либо кнопка из старого сообщения про чужого
        await _replace(callback.message, "Этот ученик больше не привязан к вам.")
        return

    logger.info("Преподаватель %s отвязал ученика %s", callback.from_user.id, student_id)
    await _replace(callback.message, f"<b>{name}</b> отвязан.", parse_mode="HTML")
    await _show_students(callback, page)


@router.callback_query(lambda c: c.data and c.data.startswith("students:list"))
async def back_to_students(callback: CallbackQuery) -> None:
    await callback.answer()
    parts = (callback.data or "").split(":")
    page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
    await _show_students(callback, page)


# ---------- /update ----------

@update_router.message(Command("update"))
async def update_cmd(message: Message) -> None:
    if config.ADMIN_IDS and message.from_user.id not in config.ADMIN_IDS:
        await message.answer("Нет доступа.")
        return

    await sheets_cache.load_all()
    n = await load_theory()
    theory_note = f" + теория ({n} тем)" if n else ""
    await message.answer(f"Кэш обновлён: Google Sheets{theory_note}.")


@update_router.message(Command("test_reminder"))
async def test_reminder_cmd(message: Message) -> None:
    if config.ADMIN_IDS and message.from_user.id not in config.ADMIN_IDS:
        await message.answer("Нет доступа.")
        return

    from database.db import get_user
    from bot.reminders import _generate_reminder

    user = await get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала зарегистрируйся в боте (/start).")
        return

    await message.answer("Генерирую тестовое напоминание…")
    try:
        text = await _generate_reminder(user.name, user.class_name)
        await message.answer(f"Вот как выглядело бы напоминание:\n\n{text}")
    except Exception as e:
        logger.exception("Ошибка при генерации тестового напоминания")
        await message.answer(f"Ошибка: {e}")


@update_router.message(Command("reindex"))
async def reindex_cmd(message: Message) -> None:
    if config.ADMIN_IDS and message.from_user.id not in config.ADMIN_IDS:
        await message.answer("Нет доступа.")
        return

    await message.answer("Перестраиваю векторную базу…")
    try:
        import asyncio
        n = await asyncio.to_thread(build_vectorstore)
        reset_retriever_cache()
        await message.answer(f"Готово — {n} чанков проиндексировано.")
    except Exception as e:
        logger.exception("Ошибка при переиндексации")
        await message.answer(f"Ошибка: {e}")
