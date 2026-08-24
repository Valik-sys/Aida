from aiogram.fsm.state import State, StatesGroup


class Onboarding(StatesGroup):
    waiting_role = State()
    waiting_subject = State()
    waiting_invite = State()


class TeacherUpload(StatesGroup):
    waiting_file = State()
    waiting_section = State()       # файл разобран, ждём выбор раздела
    waiting_section_title = State() # ждём название своего раздела
    waiting_topic = State()         # раздел выбран, ждём тему внутри него
    waiting_topic_title = State()   # ждём название своей темы


class Register(StatesGroup):
    waiting_name = State()


class ChatFlow(StatesGroup):
    waiting_question = State()


class TestFlow(StatesGroup):
    waiting_answer = State()


class TopicTestFlow(StatesGroup):
    waiting_answer = State()  # показан вопрос, ждём текстовый ввод
    waiting_next = State()    # показан результат, ждём кнопку «Далее»


class TopicAskFlow(StatesGroup):
    waiting_question = State()

