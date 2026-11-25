import os
import asyncio
import sqlite3
import re

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message

TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

bot = Bot(token=TOKEN)
dp = Dispatcher()

db = sqlite3.connect("numbers.db")
db.execute("""
CREATE TABLE IF NOT EXISTS numbers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    number TEXT NOT NULL,
    used INTEGER DEFAULT 0
)
""")
db.execute("""
CREATE TABLE IF NOT EXISTS chats (
    chat_id INTEGER PRIMARY KEY,
    enabled INTEGER DEFAULT 1
)
""")
db.commit()

KEYWORDS = ["слет", "номер", "дай номер", "нужен номер"]

def is_admin(message):
    return message.from_user and message.from_user.id == ADMIN_ID

@dp.message(Command("start"))
async def start(message: Message):
    if is_admin(message):
        await message.answer(
            "🤖 Бот запущен!\n\n"
            "/add номер — добавить номер\n"
            "/list — список номеров\n"
            "/chats — список чатов"
        )
    else:
        await message.answer("Бот готов к работе!")

@dp.message(Command("add"))
async def add_number(message: Message):
    if not is_admin(message):
        return

    number = message.text.replace("/add", "", 1).strip()
    if not number:
        await message.answer("Используй: /add +79990000000")
        return

    db.execute("INSERT INTO numbers (number) VALUES (?)", (number,))
    db.commit()
    await message.answer("✅ Номер добавлен!")

@dp.message(Command("list"))
async def list_numbers(message: Message):
    if not is_admin(message):
        return

    rows = db.execute(
        "SELECT number FROM numbers WHERE used = 0"
    ).fetchall()

    if not rows:
        await message.answer("Свободных номеров нет.")
        return

    await message.answer(
        "Свободные номера:\n" +
        "\n".join(row[0] for row in rows)
    )

@dp.message(Command("chats"))
async def chats(message: Message):
    if not is_admin(message):
        return

    rows = db.execute("SELECT chat_id, enabled FROM chats").fetchall()
    if not rows:
        await message.answer("Пока нет подключённых чатов.")
        return

    await message.answer(
        "\n".join(f"{chat_id}: {'включён' if enabled else 'выключен'}"
                  for chat_id, enabled in rows)
    )
@dp.message(Command("addchat"))
async def add_chat(message: Message):
    if not is_admin(message):
        return

    if message.chat.type == "private":
        await message.answer(
            "Добавь бота в нужную группу и отправь там /addchat"
        )
        return

    db.execute(
        "INSERT OR REPLACE INTO chats (chat_id, enabled) VALUES (?, 1)",
        (message.chat.id,)
    )
    db.commit()

    await message.answer("✅ Этот чат подключён!")
@dp.message(F.text)
async def handle_message(message: Message):
    if not message.chat or message.chat.type == "private":
        return

    text = message.text.lower()

    if not any(word in text for word in KEYWORDS):
        return

    chat = db.execute(
        "SELECT enabled FROM chats WHERE chat_id = ?",
        (message.chat.id,)
    ).fetchone()

    if not chat or not chat[0]:
        return

    cursor = db.execute(
        "SELECT id, number FROM numbers WHERE used = 0 ORDER BY id LIMIT 1"
    )
    row = cursor.fetchone()

    if not row:
        await message.reply("Свободных номеров больше нет.")
        return

    db.execute("UPDATE numbers SET used = 1 WHERE id = ?", (row[0],))
    db.commit()

    await message.reply(f"📱 Номер: {row[1]}")

async def main():
    if not TOKEN:
        raise RuntimeError("Не задан BOT_TOKEN")

    print("Бот запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
