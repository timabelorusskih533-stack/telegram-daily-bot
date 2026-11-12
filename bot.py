import asyncio
import json
import os
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton

TOKEN = os.getenv("BOT_TOKEN")
DATA_FILE = "tasks.json"

bot = Bot(token=TOKEN)
dp = Dispatcher()

def load_tasks():
    if not os.path.exists(DATA_FILE):
        return {}
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_tasks(tasks):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(tasks, f, ensure_ascii=False, indent=2)

main_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📋 Мои задачи"), KeyboardButton(text="🗑 Очистить всё")]
    ],
    resize_keyboard=True
)

@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "Привет! Я твой бот-ежедневник.\n\n"
        "• Просто напиши мне любую задачу текстом — я её запишу.\n"
        "• Отправь номер задачи (например, `1`), чтобы вычеркнуть её из списка.",
        reply_markup=main_kb
    )

@dp.message(F.text == "📋 Мои задачи")
async def show_tasks(message: Message):
    user_id = str(message.from_user.id)
    tasks = load_tasks().get(user_id, [])
    if not tasks:
        await message.answer("Список задач пока пуст. Напиши что-нибудь, чтобы добавить!")
        return
    text = "📌 **Твои задачи:**\n\n" + "\n".join(f"{i+1}. {t}" for i, t in enumerate(tasks))
    await message.answer(text, parse_mode="Markdown")

@dp.message(F.text == "🗑 Очистить всё")
async def clear_tasks(message: Message):
    user_id = str(message.from_user.id)
    all_tasks = load_tasks()
    all_tasks[user_id] = []
    save_tasks(all_tasks)
    await message.answer("Все задачи удалены!")

@dp.message(F.text)
async def handle_text(message: Message):
    user_id = str(message.from_user.id)
    all_tasks = load_tasks()
    user_tasks = all_tasks.get(user_id, [])
    text = message.text.strip()

    # Если прислали цифру — удаляем задачу под этим номером
    if text.isdigit():
        idx = int(text) - 1
        if 0 <= idx < len(user_tasks):
            removed = user_tasks.pop(idx)
            all_tasks[user_id] = user_tasks
            save_tasks(all_tasks)
            await message.answer(f"✅ Выполнено и удалено: _{removed}_", parse_mode="Markdown")
        else:
            await message.answer("Задачи с таким номером нет.")
        return

    # Иначе добавляем новую задачу
    user_tasks.append(text)
    all_tasks[user_id] = user_tasks
    save_tasks(all_tasks)
    await message.answer(f"➕ Записал: **{text}**", parse_mode="Markdown")

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())