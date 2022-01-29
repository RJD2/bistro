from asyncio import sleep
from aiogram import Bot, Dispatcher, types, executor

from config import TOKEN, questions
import keyboards as kb
from messages import MESSAGES

bot = Bot(token=TOKEN)
dp = Dispatcher(bot)


def get_answer(callback, code):
    if callback.message.reply_markup.inline_keyboard[0][0]['callback_data'] == f'button{code}':
        return f"Вы {callback.message.reply_markup.inline_keyboard[0][0]['text'].lower()}"
    return f"Вы {callback.message.reply_markup.inline_keyboard[0][1]['text'].lower()}"


@dp.message_handler(commands=['help'])
async def process_help_command(message: types.Message):
    await message.reply(MESSAGES['help'])


@dp.message_handler(commands=['start'])
async def on_message(message: types.Message):
    await message.reply(MESSAGES['start'], reply_markup=kb.round_kb)


@dp.message_handler(commands=['rm'])
async def process_rm_command(message: types.Message):
    await message.reply("Убираем шаблоны сообщений", reply_markup=kb.ReplyKeyboardRemove())


@dp.message_handler(lambda message: message.text and message.text == 'Быстротест')
async def process_command_1(message: types.Message):
    await message.reply(questions[0]['word'])
    await sleep(1)
    keyboard = kb.get_keyboard(questions[0])
    await message.reply(MESSAGES['quick2'].format(**questions[0]), reply_markup=keyboard)


@dp.callback_query_handler(lambda c: c.data and c.data.startswith('button'))
async def process_callback_button1(callback_query: types.CallbackQuery):
    code = callback_query.data[-1]
    if code.isdigit():
        code = int(code)
    await bot.answer_callback_query(callback_query.id)
    answer = get_answer(callback_query, code)
    await bot.send_message(callback_query.from_user.id, answer)


@dp.callback_query_handler(lambda c: c.data and c.data.startswith('btn'))
async def process_callback_kb1btn1(callback_query: types.CallbackQuery):
    code = callback_query.data[-1]
    if code.isdigit():
        code = int(code)
    if code == 2:
        await bot.answer_callback_query(callback_query.id, text='Нажата вторая кнопка')
    elif code == 5:
        await bot.answer_callback_query(
            callback_query.id,
            text='Нажата кнопка с номером 5.\nА этот текст может быть длиной до 200 символов 😉', show_alert=True)
    else:
        await bot.answer_callback_query(callback_query.id)
    await bot.send_message(callback_query.from_user.id, f'Нажата инлайн кнопка! code={code}')


@dp.message_handler(commands=['2'])
async def process_command_2(message: types.Message):
    await message.reply("Отправляю все возможные кнопки", reply_markup=kb.inline_kb_full)


if __name__ == '__main__':
    executor.start_polling(dp, skip_updates=True)
