from aiogram.fsm.state import State, StatesGroup


class GameStates(StatesGroup):
    waiting_for_answer = State()


class QuestionCreationStates(StatesGroup):
    waiting_for_word = State()
    waiting_for_options = State()
    waiting_for_associo = State()
    waiting_for_confirmation = State()


class BulkQuestionCreationStates(StatesGroup):
    waiting_for_words = State()
    waiting_for_options = State()
    waiting_for_associo = State()
    waiting_for_confirmation = State()


class AssocioCreationStates(StatesGroup):
    waiting_for_associo = State()
    waiting_for_confirmation = State()
