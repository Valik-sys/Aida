import time

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

import subjects as subjects_cfg
from bot.keyboards.inline import ALL_MENU_BUTTONS, student_view_kb
from bot.states.states import ChatFlow
from database.db import get_user
from database.models import ROLE_TEACHER
from rag.explainer import chat_answer


router = Router()

HISTORY_TIMEOUT = 5 * 60  # 5 минут


# Нажатия кнопок меню и команды не должны попадать в модель как вопросы:
# это и выход из режима блокировало, и стоило денег
@router.message(
    ChatFlow.waiting_question,
    ~F.text.in_(ALL_MENU_BUTTONS),
    ~F.text.startswith("/"),
)
async def ask_in_chat(message: Message, state: FSMContext) -> None:
    question = (message.text or "").strip()
    if not question:
        await message.answer("Напиши свой вопрос.")
        return

    user = await get_user(message.from_user.id)
    subject = (user.current_subject if user else None) or subjects_cfg.DEFAULT_SUBJECT
    # Преподаватель попадает сюда только из режима ученика — значит и клавиатуру
    # надо возвращать ту же, а не выбрасывать его в кабинет
    is_teacher = bool(user and user.role == ROLE_TEACHER)

    # Индекс есть только у истории. Для предмета без материалов отвечать
    # нечем — а молча отвечать из общего индекса нельзя, это был бы ответ
    # по чужому предмету.
    if subjects_cfg.is_stub(subject):
        await state.clear()
        await message.answer(
            f"По предмету «{subjects_cfg.subject_name(subject)}» материалы ещё "
            "не загружены — отвечать пока не на чем.",
            reply_markup=student_view_kb(subject, for_teacher=is_teacher),
        )
        return

    data = await state.get_data()
    history = data.get("chat_history") or []
    last_ts = data.get("last_message_ts")

    if last_ts and (time.time() - last_ts) > HISTORY_TIMEOUT:
        history = []

    waiting_msg = await message.answer("⏳ Ищу информацию...")
    answer = await chat_answer(question, history=history)

    await waiting_msg.delete()

    # Сохраняем историю (последние 10 пар, чтобы не раздувать контекст)
    history.append({"role": "user", "content": question})
    history.append({"role": "assistant", "content": answer})
    history = history[-20:]  # макс 10 пар

    await state.update_data(chat_history=history, last_message_ts=time.time())
    await message.answer(
        answer, reply_markup=student_view_kb(subject, for_teacher=is_teacher)
    )
    await state.set_state(ChatFlow.waiting_question)

