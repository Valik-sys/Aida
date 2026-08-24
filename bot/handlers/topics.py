from __future__ import annotations

import logging
import re
from typing import Any, Dict, List

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.keyboards.inline import (
    main_menu_kb,
    topics_sections_kb,
    topics_subtopics_kb,
    topics_menu_kb,
    theory_page_kb,
    topic_test_question_kb,
    topic_test_next_kb,
    topic_test_result_kb,
    topic_test_resume_kb,
    topic_card_front_kb,
    topic_card_back_kb,
    topic_card_result_kb,
    topic_ask_prompt_kb,
    topic_ask_answer_kb,
    ALL_MENU_BUTTONS,
    mistakes_kb,
    mistakes_question_kb,
    mistakes_result_kb,
    topic_final_test_question_kb,
    final_test_result_kb,
)
from database.db import (
    add_mistake,
    remove_mistake,
    get_mistakes,
    get_user,
    count_mistakes,
    mark_topic_completed,
    get_completed_topics,
    record_answer,
)
import subjects as subjects_cfg
from bot.states.states import TopicAskFlow, TopicTestFlow
from services.llm import VERDICT_CORRECT, VERDICT_UNKNOWN, verdict_of
from services.sheets import sheets_cache

# Прогресс тестов в памяти: ключ = "{user_id}:{tema_kod}"
_saved_progress: Dict[str, Dict[str, Any]] = {}
from services.theory import get_theory_pages
from services.sheets import get_topic_questions


def _get_tema_kod(section_idx: str, subtopic_idx: str) -> str:
    section_num = str(int(section_idx) + 1)
    codes = SUBTOPIC_CODES.get(section_num, [])
    idx = int(subtopic_idx)
    if idx < len(codes):
        return codes[idx]
    return f"{section_num}_{idx + 1}"

logger = logging.getLogger(__name__)

router = Router()

# Сетка разделов и тем живёт в subjects.py — там же, откуда её берёт раскладка
# файлов преподавателя. Дублировать список здесь нельзя: две копии разъедутся,
# а коды тем совпадают с колонкой «Тема_код» базового контента и с названиями
# вкладок теории, и от расхождения молча перестала бы находиться теория.
_SECTIONS_CFG = subjects_cfg.subject_sections(subjects_cfg.DEFAULT_SUBJECT)

SECTIONS = [s["full"] for s in _SECTIONS_CFG]

SUBTOPIC_CODES = {
    s["key"]: [t["key"] for t in s.get("topics") or []] for s in _SECTIONS_CFG
}

SUBTOPICS = {
    s["key"]: [t["title"] for t in s.get("topics") or []] for s in _SECTIONS_CFG
}


@router.callback_query(lambda c: c.data == "topics:start")
async def topics_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.message.edit_text(
        "📚 Выбери раздел:",
        reply_markup=topics_sections_kb(SECTIONS),
    )
    await callback.answer()


@router.callback_query(lambda c: c.data.startswith("topics:section:"))
async def topics_section(callback: CallbackQuery, state: FSMContext) -> None:
    section_idx = callback.data.split(":")[-1]
    section_num = str(int(section_idx) + 1)
    section_name = SECTIONS[int(section_idx)]
    subtopics = SUBTOPICS.get(section_num, [])

    all_codes = SUBTOPIC_CODES.get(section_num, [])
    completed = await get_completed_topics(callback.from_user.id, section_num)
    final_unlocked = bool(all_codes) and all(c in completed for c in all_codes)

    await state.update_data(current_section=section_idx, current_section_name=section_name)
    await callback.message.edit_text(
        f"📚 {section_name}\n\nВыбери тему:",
        reply_markup=topics_subtopics_kb(subtopics, section_idx, final_unlocked=final_unlocked),
    )
    await callback.answer()


@router.callback_query(lambda c: c.data.startswith("topics:subtopic:"))
async def topics_subtopic(callback: CallbackQuery, state: FSMContext) -> None:
    parts = callback.data.split(":")
    section_idx = parts[2]
    subtopic_idx = parts[3]
    section_num = str(int(section_idx) + 1)
    subtopic_name = SUBTOPICS.get(section_num, [])[int(subtopic_idx)]
    await state.update_data(current_subtopic=subtopic_name)
    await callback.message.edit_text(
        f"📖 {subtopic_name}\n\nЧто хочешь делать?",
        reply_markup=topics_menu_kb(section_idx, subtopic_idx),
    )
    await callback.answer()


@router.callback_query(lambda c: c.data.startswith("topics:theory:"))
async def topics_theory(callback: CallbackQuery, state: FSMContext) -> None:
    parts = callback.data.split(":")
    section_idx, subtopic_idx = parts[2], parts[3]
    data = await state.get_data()
    subtopic = data.get("current_subtopic", "")

    tema_kod = _get_tema_kod(section_idx, subtopic_idx)
    pages = get_theory_pages(tema_kod) or get_theory_pages(subtopic)
    if not pages:
        await callback.message.edit_text(
            f"📖 {subtopic}\n\nТеория по этой теме пока не добавлена.",
            reply_markup=topics_menu_kb(section_idx, subtopic_idx),
        )
        await callback.answer()
        return

    await callback.message.edit_text(
        f"📖 {subtopic}\n\n{pages[0]}",
        reply_markup=theory_page_kb(section_idx, subtopic_idx, 0, len(pages)),
    )
    await callback.answer()


@router.callback_query(lambda c: c.data.startswith("topics:theory_page:"))
async def topics_theory_page(callback: CallbackQuery, state: FSMContext) -> None:
    parts = callback.data.split(":")
    section_idx, subtopic_idx, page = parts[2], parts[3], int(parts[4])
    data = await state.get_data()
    subtopic = data.get("current_subtopic", "")

    tema_kod = _get_tema_kod(section_idx, subtopic_idx)
    pages = get_theory_pages(tema_kod) or get_theory_pages(subtopic)
    if not pages or page >= len(pages):
        await callback.answer()
        return

    await callback.message.edit_text(
        f"📖 {subtopic}\n\n{pages[page]}",
        reply_markup=theory_page_kb(section_idx, subtopic_idx, page, len(pages)),
    )
    await callback.answer()


@router.callback_query(lambda c: c.data == "noop")
async def noop_handler(callback: CallbackQuery) -> None:
    await callback.answer()


def _card_texts(card: Dict) -> tuple[str, str]:
    """Возвращает (лицевая сторона, ответ)."""
    cat = card.get("_cat", "")
    if cat == "dates":
        return card.get("Событие", ""), f"📅 {card.get('Дата', '')}"
    elif cat == "concepts":
        return card.get("Определение", ""), f"📖 {card.get('Понятие', '')}"
    else:
        return card.get("Роль в истории", ""), f"👤 {card.get('Личность', '')}"


async def _show_topic_card(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    session = data.get("topic_cards_session") or {}
    queue: List[str] = session.get("queue") or []
    cards_by_id: Dict = session.get("cards_by_id") or {}
    section_idx = session.get("section_idx", "0")
    subtopic_idx = session.get("subtopic_idx", "0")
    msg_id = session.get("msg_id")
    total = session.get("total", 0)
    done = total - len(queue)

    if not queue:
        return

    card_id = queue[0]
    card = cards_by_id.get(card_id) or {}
    front, _ = _card_texts(card)
    text = f"🎴 {done + 1} / {total}\n\n{front}"
    kb = topic_card_front_kb(card_id, section_idx, subtopic_idx)
    try:
        await callback.bot.edit_message_text(
            chat_id=callback.message.chat.id,
            message_id=msg_id,
            text=text,
            reply_markup=kb,
        )
    except Exception:
        sent = await callback.bot.send_message(
            chat_id=callback.message.chat.id, text=text, reply_markup=kb
        )
        session["msg_id"] = sent.message_id
        await state.update_data(topic_cards_session=session)


@router.callback_query(lambda c: c.data.startswith("topics:cards:"))
async def topics_cards(callback: CallbackQuery, state: FSMContext) -> None:
    import random
    parts = callback.data.split(":")
    section_idx, subtopic_idx = parts[2], parts[3]
    section_num = str(int(section_idx) + 1)
    data = await state.get_data()
    subtopic = data.get("current_subtopic", "")

    all_cards: List[Dict] = []
    for card in (sheets_cache.flash_dates or []):
        if str(card.get("Раздел", "")).strip() == section_num:
            all_cards.append({**card, "_cat": "dates"})
    for card in (sheets_cache.flash_concepts or []):
        if str(card.get("Раздел", "")).strip() == section_num:
            all_cards.append({**card, "_cat": "concepts"})
    for card in (sheets_cache.flash_persons or []):
        if str(card.get("Раздел", "")).strip() == section_num:
            all_cards.append({**card, "_cat": "persons"})

    if not all_cards:
        await callback.message.edit_text(
            f"🎴 {subtopic}\n\nКарточек по этому разделу пока нет.",
            reply_markup=topics_menu_kb(section_idx, subtopic_idx),
        )
        await callback.answer()
        return

    random.shuffle(all_cards)
    cards_by_id = {str(i): c for i, c in enumerate(all_cards)}
    session: Dict[str, Any] = {
        "queue": list(cards_by_id.keys()),
        "all_ids": list(cards_by_id.keys()),
        "cards_by_id": cards_by_id,
        "total": len(all_cards),
        "msg_id": callback.message.message_id,
        "section_idx": section_idx,
        "subtopic_idx": subtopic_idx,
    }
    await state.update_data(topic_cards_session=session)
    await _show_topic_card(callback, state)
    await callback.answer()


@router.callback_query(lambda c: c.data.startswith("topic_card:reveal:"))
async def topic_card_reveal(callback: CallbackQuery, state: FSMContext) -> None:
    card_id = callback.data.split(":")[-1]
    data = await state.get_data()
    session = data.get("topic_cards_session") or {}
    queue: List[str] = session.get("queue") or []
    cards_by_id: Dict = session.get("cards_by_id") or {}
    section_idx = session.get("section_idx", "0")
    subtopic_idx = session.get("subtopic_idx", "0")
    msg_id = session.get("msg_id")
    total = session.get("total", 0)
    done = total - len(queue)

    if not queue or queue[0] != card_id:
        await callback.answer()
        return

    card = cards_by_id.get(card_id) or {}
    front, answer = _card_texts(card)
    text = f"🎴 {done + 1} / {total}\n\n{front}\n\n<b>Ответ: {answer}</b>"
    kb = topic_card_back_kb(card_id, section_idx, subtopic_idx)
    try:
        await callback.bot.edit_message_text(
            chat_id=callback.message.chat.id,
            message_id=msg_id,
            text=text,
            parse_mode="HTML",
            reply_markup=kb,
        )
    except Exception:
        pass
    await callback.answer()


@router.callback_query(lambda c: c.data.startswith("topic_card:mark:"))
async def topic_card_mark(callback: CallbackQuery, state: FSMContext) -> None:
    parts = callback.data.split(":")
    action, card_id = parts[2], parts[3]
    data = await state.get_data()
    session = data.get("topic_cards_session") or {}
    queue: List[str] = session.get("queue") or []
    section_idx = session.get("section_idx", "0")
    subtopic_idx = session.get("subtopic_idx", "0")
    msg_id = session.get("msg_id")

    if not queue or queue[0] != card_id:
        await callback.answer()
        return

    queue.pop(0)
    if action == "unknown":
        queue.append(card_id)
    session["queue"] = queue
    await state.update_data(topic_cards_session=session)

    if not queue:
        total = session.get("total", 0)
        text = f"🎴 Готово!\n\nПрошёл все {total} карточек."
        kb = topic_card_result_kb(section_idx, subtopic_idx)
        try:
            await callback.bot.edit_message_text(
                chat_id=callback.message.chat.id,
                message_id=msg_id,
                text=text,
                reply_markup=kb,
            )
        except Exception:
            pass
        await callback.answer()
        return

    await _show_topic_card(callback, state)
    await callback.answer()


@router.callback_query(lambda c: c.data == "topic_card:finish")
async def topic_card_finish(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    session = data.get("topic_cards_session") or {}
    queue: List[str] = session.get("queue") or []
    total = session.get("total", 0)
    section_idx = session.get("section_idx", "0")
    subtopic_idx = session.get("subtopic_idx", "0")
    msg_id = session.get("msg_id")
    done = total - len(queue)
    text = f"🎴 Остановился на {done} из {total}."
    kb = topic_card_result_kb(section_idx, subtopic_idx)
    try:
        await callback.bot.edit_message_text(
            chat_id=callback.message.chat.id,
            message_id=msg_id,
            text=text,
            reply_markup=kb,
        )
    except Exception:
        pass
    await callback.answer()


@router.callback_query(lambda c: c.data == "topic_card:retry")
async def topic_card_retry(callback: CallbackQuery, state: FSMContext) -> None:
    import random
    data = await state.get_data()
    session = data.get("topic_cards_session") or {}
    all_ids: List[str] = session.get("all_ids") or list((session.get("cards_by_id") or {}).keys())
    random.shuffle(all_ids)
    session["queue"] = all_ids
    session["all_ids"] = all_ids
    session["msg_id"] = callback.message.message_id
    await state.update_data(topic_cards_session=session)
    await _show_topic_card(callback, state)
    await callback.answer()


@router.callback_query(lambda c: c.data.startswith("topic_card:back:"))
async def topic_card_back(callback: CallbackQuery, state: FSMContext) -> None:
    parts = callback.data.split(":")
    section_idx, subtopic_idx = parts[2], parts[3]
    section_num = str(int(section_idx) + 1)
    subtopic_name = SUBTOPICS.get(section_num, [])[int(subtopic_idx)]
    await state.update_data(topic_cards_session=None, current_subtopic=subtopic_name)
    await callback.message.edit_text(
        f"📖 {subtopic_name}\n\nЧто хочешь делать?",
        reply_markup=topics_menu_kb(section_idx, subtopic_idx),
    )
    await callback.answer()


@router.callback_query(lambda c: c.data.startswith("topics:test:"))
async def topics_test(callback: CallbackQuery, state: FSMContext) -> None:
    parts = callback.data.split(":")
    section_idx, subtopic_idx = parts[2], parts[3]
    data = await state.get_data()
    subtopic = data.get("current_subtopic", "")

    tema_kod = _get_tema_kod(section_idx, subtopic_idx)
    user_id = callback.from_user.id
    progress_key = f"{user_id}:{tema_kod}"
    saved = _saved_progress.get(progress_key)

    if saved:
        resume_idx = saved["idx"]
        questions = saved["questions"]
        await state.update_data(topic_test_pending=saved)
        await callback.message.edit_text(
            f"📝 {subtopic}\n\nУ тебя есть незавершённый тест.",
            reply_markup=topic_test_resume_kb(section_idx, subtopic_idx, resume_idx, len(questions)),
        )
        await callback.answer()
        return

    questions = get_topic_questions(tema_kod)
    if not questions:
        await callback.message.edit_text(
            f"📝 {subtopic}\n\nВопросов по этой теме пока нет.",
            reply_markup=topics_menu_kb(section_idx, subtopic_idx),
        )
        await callback.answer()
        return

    session: Dict[str, Any] = {
        "questions": questions, "idx": 0, "wrong": [],
        "msg_id": callback.message.message_id,
        "section_idx": section_idx, "subtopic_idx": subtopic_idx,
        "tema_kod": tema_kod, "user_id": user_id,
    }
    await state.update_data(topic_test_session=session)
    await _show_topic_question(callback.message, state, idx=0)
    await state.set_state(TopicTestFlow.waiting_answer)
    await callback.answer()


@router.callback_query(lambda c: c.data == "topic_test:resume")
async def topic_test_resume(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    saved = data.get("topic_test_pending")
    if not saved:
        await callback.answer("Прогресс не найден.")
        return
    session = {**saved, "msg_id": callback.message.message_id}
    await state.update_data(topic_test_session=session)
    await _show_topic_question(callback.message, state, idx=session["idx"])
    await state.set_state(TopicTestFlow.waiting_answer)
    await callback.answer()


@router.callback_query(lambda c: c.data == "topic_test:fresh")
async def topic_test_fresh(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    saved = data.get("topic_test_pending") or {}
    tema_kod = saved.get("tema_kod", "")
    user_id = callback.from_user.id
    _saved_progress.pop(f"{user_id}:{tema_kod}", None)

    questions = saved.get("questions") or get_topic_questions(tema_kod)
    if not questions:
        await callback.answer("Не удалось загрузить вопросы.")
        return

    session: Dict[str, Any] = {
        **saved,
        "idx": 0, "wrong": [],
        "msg_id": callback.message.message_id,
        "user_id": user_id,
    }
    await state.update_data(topic_test_session=session)
    await _show_topic_question(callback.message, state, idx=0)
    await state.set_state(TopicTestFlow.waiting_answer)
    await callback.answer()


async def _show_topic_question(message: Any, state: FSMContext, *, idx: int, prev_result: str = "") -> None:
    from bot.handlers.tests import _format_question
    data = await state.get_data()
    session = data.get("topic_test_session") or {}

    # Удаляем накопленные подсказки о формате перед показом следующего вопроса
    hint_ids: List[int] = session.pop("hint_msg_ids", None) or []
    if hint_ids:
        await state.update_data(topic_test_session=session)
        for hid in hint_ids:
            try:
                await message.bot.delete_message(chat_id=message.chat.id, message_id=hid)
            except Exception:
                pass

    questions: List[Dict[str, Any]] = session.get("questions") or []
    msg_id = session.get("msg_id")
    s_idx = session.get("section_idx", "0")
    st_idx = session.get("subtopic_idx", "0")
    if idx >= len(questions):
        return
    q = questions[idx]
    q_text = _format_question(q, idx + 1, len(questions))
    text = f"{prev_result}\n{'─' * 20}\n{q_text}" if prev_result else q_text
    if session.get("is_mistakes_mode"):
        kb = mistakes_question_kb()
    elif session.get("is_final_test"):
        kb = topic_final_test_question_kb(s_idx)
    else:
        kb = topic_test_question_kb(s_idx, st_idx)
    try:
        await message.bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=msg_id,
            text=text,
            reply_markup=kb,
        )
    except Exception:
        sent = await message.bot.send_message(chat_id=message.chat.id, text=text, reply_markup=kb)
        session["msg_id"] = sent.message_id
        await state.update_data(topic_test_session=session)


@router.message(
    TopicTestFlow.waiting_answer,
    ~F.text.in_(ALL_MENU_BUTTONS),
    ~F.text.startswith("/"),
)
async def topic_test_answer(message: Message, state: FSMContext) -> None:
    from bot.handlers.tests import (
        _get_part_b_input_type, normalize_part_b_input, _build_part_b_prompt,
        _detect_part_b_kind, _normalize_expected_part_b, _normalize_year_input,
        _detect_hint, _format_question, _check_partial_credit,
    )
    from services.llm import check_test_answer

    data = await state.get_data()
    session = data.get("topic_test_session") or {}
    questions: List[Dict[str, Any]] = session.get("questions") or []
    idx = session.get("idx") or 0
    msg_id = session.get("msg_id")

    if idx >= len(questions):
        await state.clear()
        return

    q = questions[idx]
    raw_text = (message.text or "").strip()
    if not raw_text or not re.search(r"[a-zA-Zа-яА-ЯёЁ0-9]", raw_text):
        try:
            await message.delete()
        except Exception:
            pass
        sent = await message.bot.send_message(chat_id=message.chat.id, text=_detect_hint(q))
        hint_ids = session.get("hint_msg_ids") or []
        hint_ids.append(sent.message_id)
        session["hint_msg_ids"] = hint_ids
        await state.update_data(topic_test_session=session)
        return

    student_answer = raw_text
    expected = q["expected"]

    if q["type"] == "A":
        options = q.get("options") or {}
        seen: set = set()
        digits = [int(ch) for ch in raw_text if ch.isdigit() and 1 <= int(ch) <= 5 and int(ch) not in seen and not seen.add(int(ch))]  # type: ignore[func-returns-value]
        if digits:
            parts_list = []
            for d in digits:
                opt_text = (options.get(d) or "").strip()
                parts_list.append(f"{d}) {opt_text}" if opt_text else str(d))
            student_answer = "; ".join(parts_list)

    correct_for_llm = expected
    if q["type"] == "A" and expected:
        options = q.get("options") or {}
        digits = [int(ch) for ch in expected if ch.isdigit()]
        if len(digits) > 1:
            correct_for_llm = "; ".join(f"{d}) {options.get(d, '')}" if options.get(d) else str(d) for d in digits)
        elif len(digits) == 1:
            opt_text = options.get(digits[0], "")
            if opt_text:
                correct_for_llm = f"{digits[0]}) {opt_text}"
    elif q["type"] == "B":
        part_b_input_type = _get_part_b_input_type(q)
        if part_b_input_type:
            ok, normalized = normalize_part_b_input(raw_text, part_b_input_type)
            if not ok:
                try:
                    await message.delete()
                except Exception:
                    pass
                sent = await message.bot.send_message(chat_id=message.chat.id, text=_build_part_b_prompt(part_b_input_type))
                hint_ids = session.get("hint_msg_ids") or []
                hint_ids.append(sent.message_id)
                session["hint_msg_ids"] = hint_ids
                await state.update_data(topic_test_session=session)
                return
            student_answer = normalized
            if expected:
                correct_for_llm = _normalize_expected_part_b(expected)
        elif _detect_part_b_kind(q) == "year_open":
            ok, normalized_year = _normalize_year_input(raw_text)
            if not ok:
                try:
                    await message.delete()
                except Exception:
                    pass
                sent = await message.bot.send_message(chat_id=message.chat.id, text="Напиши год в виде 3–4 цифр, например: 1900.")
                hint_ids = session.get("hint_msg_ids") or []
                hint_ids.append(sent.message_id)
                session["hint_msg_ids"] = hint_ids
                await state.update_data(topic_test_session=session)
                return
            student_answer = normalized_year
            if expected:
                ok_exp, norm_exp = _normalize_year_input(expected)
                correct_for_llm = norm_exp if ok_exp else expected.strip()

    partial = _check_partial_credit(q, raw_text)
    if partial:
        llm_response = partial
    else:
        llm_response = await check_test_answer(
            question_text=q["question_text"],
            student_answer=student_answer,
            correct_answer=correct_for_llm,
        )

    is_wrong = llm_response.startswith("❌")
    user_id = session.get("user_id") or message.from_user.id
    is_mistakes_mode = session.get("is_mistakes_mode", False)

    # Повторение ошибок — такая же практика, как обычная тренировка: без записи
    # ответы отсюда не попадали бы ни в статистику, ни в расписание повторов,
    # и досчитать их потом было бы не из чего
    verdict = verdict_of(llm_response)
    if verdict != VERDICT_UNKNOWN:
        try:
            user = await get_user(user_id)
            await record_answer(
                user_id,
                q.get("question_text", ""),
                is_correct=verdict == VERDICT_CORRECT,
                teacher_id=user.teacher_id if user else None,
                subject=(user.current_subject if user else "") or "",
                student_answer=raw_text,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Не удалось записать ответ ученика %s", user_id)

    if is_wrong:
        wrong = session.get("wrong") or []
        wrong.append(idx)
        session["wrong"] = wrong
        if not is_mistakes_mode:
            await add_mistake(user_id, q, session.get("tema_kod", ""))
    elif is_mistakes_mode:
        qhash = q.get("question_hash", "")
        if qhash:
            await remove_mistake(user_id, qhash)

    try:
        await message.delete()
    except Exception:
        pass

    s_idx = session.get("section_idx", "0")
    st_idx = session.get("subtopic_idx", "0")
    is_last = (idx + 1 >= len(questions))

    result_line = llm_response

    if not is_last:
        session["idx"] = idx + 1
        await state.update_data(topic_test_session=session)
        await _show_topic_question(message, state, idx=idx + 1, prev_result=result_line)
        await state.set_state(TopicTestFlow.waiting_answer)
    else:
        wrong = session.get("wrong") or []
        correct = len(questions) - len(wrong)
        if is_mistakes_mode:
            remaining = await count_mistakes(user_id)
            fixed = correct
            result_text = (
                f"📊 Вопрос {idx + 1} / {len(questions)}\n\n{llm_response}\n\n"
                f"🏁 Исправлено: {fixed} из {len(questions)}\n"
                + (f"Осталось ошибок: {remaining}" if remaining else "✅ Все ошибки исправлены!")
            )
            kb = mistakes_result_kb(has_remaining=remaining > 0)
        elif session.get("is_final_test"):
            result_text = (
                f"📊 Вопрос {idx + 1} / {len(questions)}\n\n{llm_response}\n\n"
                f"🏆 Итоговый тест завершён!\n"
                f"Результат: {correct} из {len(questions)}"
            )
            kb = final_test_result_kb(s_idx)
        else:
            section_num = str(int(s_idx) + 1)
            tema_kod = session.get("tema_kod", "")
            if tema_kod:
                await mark_topic_completed(user_id, section_num, tema_kod)
            result_text = f"📊 Вопрос {idx + 1} / {len(questions)}\n\n{llm_response}\n\n🏁 Итог: {correct} из {len(questions)}"
            progress_key = f"{session.get('user_id')}:{session.get('tema_kod')}"
            _saved_progress.pop(progress_key, None)
            kb = topic_test_result_kb(s_idx, st_idx)
        try:
            await message.bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=msg_id,
                text=result_text,
                reply_markup=kb,
            )
        except Exception:
            sent = await message.bot.send_message(
                chat_id=message.chat.id,
                text=result_text,
                reply_markup=kb,
            )
            session["msg_id"] = sent.message_id
        session["idx"] = idx
        await state.update_data(topic_test_session=session)
        await state.set_state(None)


@router.message(
    TopicTestFlow.waiting_next,
    ~F.text.in_(ALL_MENU_BUTTONS),
    ~F.text.startswith("/"),
)
async def topic_test_waiting_next(message: Message, state: FSMContext) -> None:
    try:
        await message.delete()
    except Exception:
        pass
    await message.bot.send_message(
        chat_id=message.chat.id,
        text="Нажми ➡️ Далее чтобы перейти к следующему вопросу.",
    )


@router.callback_query(lambda c: c.data == "topic_test:next")
async def topic_test_next(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    session = data.get("topic_test_session") or {}
    idx = (session.get("idx") or 0) + 1
    session["idx"] = idx
    await state.update_data(topic_test_session=session)
    await _show_topic_question(callback.message, state, idx=idx)
    await state.set_state(TopicTestFlow.waiting_answer)
    await callback.answer()


@router.callback_query(lambda c: c.data == "topic_test:retry")
async def topic_test_retry(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    session = data.get("topic_test_session") or {}
    questions = session.get("questions") or []
    if not questions:
        await state.clear()
        await callback.answer()
        return
    # Сбрасываем сохранённый прогресс
    tema_kod = session.get("tema_kod", "")
    user_id = session.get("user_id") or callback.from_user.id
    if tema_kod:
        _saved_progress.pop(f"{user_id}:{tema_kod}", None)
    session["idx"] = 0
    session["wrong"] = []
    session["msg_id"] = callback.message.message_id
    await state.update_data(topic_test_session=session)
    await _show_topic_question(callback.message, state, idx=0)
    await state.set_state(TopicTestFlow.waiting_answer)
    await callback.answer()


@router.callback_query(lambda c: c.data.startswith("topic_test:back:"))
async def topic_test_back(callback: CallbackQuery, state: FSMContext) -> None:
    parts = callback.data.split(":")
    section_idx, subtopic_idx = parts[2], parts[3]
    section_num = str(int(section_idx) + 1)
    subtopic_name = SUBTOPICS.get(section_num, [])[int(subtopic_idx)]

    # Сохраняем прогресс если тест не завершён
    data = await state.get_data()
    session = data.get("topic_test_session") or {}
    idx = session.get("idx", 0)
    questions = session.get("questions") or []
    tema_kod = session.get("tema_kod", "")
    user_id = session.get("user_id") or callback.from_user.id
    # В состоянии waiting_next пользователь уже ответил на idx, следующий — idx+1
    current_fsm = await state.get_state()
    resume_idx = idx + 1 if current_fsm == TopicTestFlow.waiting_next.state else idx
    if tema_kod and questions and 0 <= resume_idx < len(questions):
        _saved_progress[f"{user_id}:{tema_kod}"] = {**session, "idx": resume_idx, "msg_id": None}

    await state.clear()
    await state.update_data(current_subtopic=subtopic_name)
    await callback.message.edit_text(
        f"📖 {subtopic_name}\n\nЧто хочешь делать?",
        reply_markup=topics_menu_kb(section_idx, subtopic_idx),
    )
    await callback.answer()


@router.callback_query(lambda c: c.data.startswith("topics:ask:"))
async def topics_ask(callback: CallbackQuery, state: FSMContext) -> None:
    parts = callback.data.split(":")
    section_idx, subtopic_idx = parts[2], parts[3]
    data = await state.get_data()
    subtopic = data.get("current_subtopic", "")

    await state.update_data(
        topic_ask_section=section_idx,
        topic_ask_subtopic_idx=subtopic_idx,
        topic_ask_msg_id=callback.message.message_id,
        topic_ask_history=[],
    )
    await callback.message.edit_text(
        f"💬 {subtopic}\n\nНапиши вопрос по этой теме 👇",
        reply_markup=topic_ask_prompt_kb(section_idx, subtopic_idx),
    )
    await state.set_state(TopicAskFlow.waiting_question)
    await callback.answer()


@router.message(
    TopicAskFlow.waiting_question,
    ~F.text.in_(ALL_MENU_BUTTONS),
    ~F.text.startswith("/"),
)
async def topic_ask_answer(message: Message, state: FSMContext) -> None:
    from rag.explainer import chat_answer

    question = (message.text or "").strip()
    if not question:
        try:
            await message.delete()
        except Exception:
            pass
        return

    data = await state.get_data()
    section_idx = data.get("topic_ask_section", "0")
    subtopic_idx = data.get("topic_ask_subtopic_idx", "0")
    msg_id = data.get("topic_ask_msg_id")
    history = data.get("topic_ask_history") or []
    subtopic = data.get("current_subtopic", "")

    try:
        await message.delete()
    except Exception:
        pass

    try:
        await message.bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=msg_id,
            text="⏳ Ищу информацию...",
        )
    except Exception:
        pass

    contextualized_question = f"[Тема: {subtopic}] {question}" if subtopic else question
    answer = await chat_answer(contextualized_question, history=history)

    history.append({"role": "user", "content": question})
    history.append({"role": "assistant", "content": answer})
    history = history[-20:]
    await state.update_data(topic_ask_history=history)

    result_text = f"💬 {subtopic}\n\n❓ {question}\n\n{answer}\n\n{'─' * 20}\nЗадай следующий вопрос 👇"
    kb = topic_ask_answer_kb(section_idx, subtopic_idx)
    try:
        await message.bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=msg_id,
            text=result_text,
            reply_markup=kb,
        )
    except Exception:
        sent = await message.bot.send_message(
            chat_id=message.chat.id,
            text=result_text,
            reply_markup=kb,
        )
        await state.update_data(topic_ask_msg_id=sent.message_id)

    await state.set_state(TopicAskFlow.waiting_question)


@router.callback_query(lambda c: c.data.startswith("topic_ask:back:"))
async def topic_ask_back(callback: CallbackQuery, state: FSMContext) -> None:
    parts = callback.data.split(":")
    section_idx, subtopic_idx = parts[2], parts[3]
    section_num = str(int(section_idx) + 1)
    subtopic_name = SUBTOPICS.get(section_num, [])[int(subtopic_idx)]
    await state.clear()
    await state.update_data(current_subtopic=subtopic_name)
    await callback.message.edit_text(
        f"📖 {subtopic_name}\n\nЧто хочешь делать?",
        reply_markup=topics_menu_kb(section_idx, subtopic_idx),
    )
    await callback.answer()


@router.callback_query(lambda c: c.data == "mistakes:start")
async def mistakes_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    n = await count_mistakes(callback.from_user.id)
    if n == 0:
        text = "❌ Мои ошибки\n\n✅ Ошибок нет — ты всё знаешь!"
    else:
        text = f"❌ Мои ошибки\n\nНакоплено вопросов: {n}\n\nПройди их заново — правильные ответы исчезнут из списка."
    await callback.message.edit_text(text, reply_markup=mistakes_kb())
    await callback.answer()


@router.callback_query(lambda c: c.data == "topics:final_test_locked")
async def topics_final_test_locked(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer("Сначала пройди тест по каждой теме этого раздела!", show_alert=True)


@router.callback_query(lambda c: c.data.startswith("topics:final_test:"))
async def topics_final_test(callback: CallbackQuery, state: FSMContext) -> None:
    import random as _random
    section_idx = callback.data.split(":")[-1]
    section_num = str(int(section_idx) + 1)
    user_id = callback.from_user.id

    all_codes = SUBTOPIC_CODES.get(section_num, [])
    completed = await get_completed_topics(user_id, section_num)
    if not all(c in completed for c in all_codes):
        await callback.answer("Сначала пройди тест по каждой теме этого раздела!", show_alert=True)
        return

    all_a: List[Dict[str, Any]] = []
    all_b: List[Dict[str, Any]] = []
    for tema_kod in all_codes:
        for q in get_topic_questions(tema_kod):
            (all_a if q["type"] == "A" else all_b).append(q)

    _random.shuffle(all_a)
    _random.shuffle(all_b)
    questions = all_a[:16] + all_b[:22]

    if not questions:
        await callback.answer("Нет вопросов для итогового теста.", show_alert=True)
        return

    section_name = SECTIONS[int(section_idx)]
    session: Dict[str, Any] = {
        "questions": questions,
        "idx": 0,
        "wrong": [],
        "msg_id": callback.message.message_id,
        "section_idx": section_idx,
        "subtopic_idx": "0",
        "tema_kod": f"final_{section_num}",
        "user_id": user_id,
        "is_final_test": True,
        "section_name": section_name,
    }
    await state.update_data(topic_test_session=session)
    await _show_topic_question(callback.message, state, idx=0)
    await state.set_state(TopicTestFlow.waiting_answer)
    await callback.answer()


@router.callback_query(lambda c: c.data == "mistakes:repeat")
async def mistakes_repeat(callback: CallbackQuery, state: FSMContext) -> None:
    user_id = callback.from_user.id
    questions = await get_mistakes(user_id)
    if not questions:
        await callback.message.edit_text(
            "✅ Ошибок нет — ты всё знаешь!",
            reply_markup=mistakes_kb(),
        )
        await callback.answer()
        return
    session: Dict[str, Any] = {
        "questions": questions,
        "idx": 0,
        "wrong": [],
        "msg_id": callback.message.message_id,
        "is_mistakes_mode": True,
        "user_id": user_id,
        "section_idx": "0",
        "subtopic_idx": "0",
        "tema_kod": "",
    }
    await state.update_data(topic_test_session=session)
    await state.set_state(TopicTestFlow.waiting_answer)
    await _show_topic_question(callback.message, state, idx=0)
    await callback.answer()
