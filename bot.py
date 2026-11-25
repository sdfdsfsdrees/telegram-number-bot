import os
import re
import asyncio
import sqlite3

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton
)

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

if not TOKEN or not ADMIN_ID:
    raise RuntimeError("Проверь BOT_TOKEN и ADMIN_ID в .env")

bot = Bot(TOKEN)
dp = Dispatcher()
db = sqlite3.connect("numbers.db", check_same_thread=False)
db.execute("PRAGMA journal_mode=WAL")
db.execute("""CREATE TABLE IF NOT EXISTS numbers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    number TEXT NOT NULL,
    used INTEGER DEFAULT 0
)""")
db.execute("""CREATE TABLE IF NOT EXISTS chats (
    chat_id INTEGER PRIMARY KEY,
    title TEXT,
    enabled INTEGER DEFAULT 1
)""")
db.commit()

lock = asyncio.Lock()


def admin(user_id):
    return user_id == ADMIN_ID


def panel():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💬 Чаты", callback_data="chats"),
            InlineKeyboardButton(text="📱 Номера", callback_data="numbers")
        ],
        [
            InlineKeyboardButton(text="📊 Статистика", callback_data="stats"),
            InlineKeyboardButton(text="➕ Подключить чат", callback_data="help")
        ]
    ])


def chat_buttons():
    rows = db.execute(
        "SELECT chat_id, title, enabled FROM chats"
    ).fetchall()

    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text=f"{'🟢' if enabled else '🔴'} {title or chat_id}",
                callback_data=f"toggle:{chat_id}"
            ),
            InlineKeyboardButton(
                text="❌",
                callback_data=f"remove:{chat_id}"
            )
        ]
        for chat_id, title, enabled in rows
    ])


@dp.message(Command("start", "panel"))
async def start(m: Message):
    if admin(m.from_user.id):
        await m.answer("🤖 Number Bot 2.0\nПанель управления:", reply_markup=panel())
    else:
        await m.answer("Бот работает.")


@dp.callback_query()
async def buttons(c: CallbackQuery):
    if not admin(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True)
        return

    data = c.data

    if data == "chats":
        await c.message.answer("💬 Подключённые чаты:", reply_markup=chat_buttons())

    elif data == "numbers":
        total, free = db.execute(
            "SELECT COUNT(*), SUM(used = 0) FROM numbers"
        ).fetchone()
        await c.message.answer(
            f"📱 Всего: {total}\n"
            f"Свободно: {free or 0}\n"
            f"Выдано: {total - (free or 0)}\n\n"
            "/add номер\n/addmany\n/list\n/delete ID"
        )

    elif data == "stats":
        total, free = db.execute(
            "SELECT COUNT(*), SUM(used = 0) FROM numbers"
        ).fetchone()
        chats = db.execute(
            "SELECT COUNT(*) FROM chats WHERE enabled = 1"
        ).fetchone()[0]
        await c.message.answer(
            f"📊 Статистика\n"
            f"Номеров: {total}\n"
            f"Свободно: {free or 0}\n"
            f"Активных чатов: {chats}"
        )

    elif data == "help":
        await c.message.answer(
            "Добавь бота в группу и отправь там /addchat."
        )

    elif data.startswith("toggle:"):
        chat_id = int(data.split(":")[1])
        db.execute(
            "UPDATE chats SET enabled = 1 - enabled WHERE chat_id = ?",
            (chat_id,)
        )
        db.commit()
        await c.message.edit_reply_markup(reply_markup=chat_buttons())

    elif data.startswith("remove:"):
        chat_id = int(data.split(":")[1])
        db.execute("DELETE FROM chats WHERE chat_id = ?", (chat_id,))
        db.commit()
        await c.message.edit_reply_markup(reply_markup=chat_buttons())

    await c.answer()


@dp.message(Command("addchat"))
async def add_chat(m: Message):
    if not admin(m.from_user.id):
        return

    if m.chat.type == "private":
        await m.answer("Добавь бота в группу и отправь /addchat внутри неё.")
        return

    db.execute(
        """INSERT INTO chats (chat_id, title, enabled)
        VALUES (?, ?, 1)
        ON CONFLICT(chat_id) DO UPDATE SET
        title = excluded.title, enabled = 1""",
        (m.chat.id, m.chat.title or "Группа")
    )
    db.commit()
    await m.answer("✅ Чат подключён!")


@dp.message(Command("add"))
async def add_number(m: Message):
    if not admin(m.from_user.id) or m.chat.type != "private":
        return

    parts = m.text.split(maxsplit=1)
    if len(parts) < 2:
        await m.answer("Используй: /add +79990000000")
        return

    number = parts[1].strip()
    if not number:
        return

    db.execute("INSERT INTO numbers (number) VALUES (?)", (number,))
    db.commit()
    await m.answer("✅ Номер добавлен!")


@dp.message(Command("addmany"))
async def add_many(m: Message):
    if not admin(m.from_user.id) or m.chat.type != "private":
        return

    lines = m.text.splitlines()[1:]
    numbers = list(dict.fromkeys(x.strip() for x in lines if x.strip()))

    added = 0
    for number in numbers:
        exists = db.execute(
            "SELECT 1 FROM numbers WHERE number = ?", (number,)
        ).fetchone()
        if not exists:
            db.execute("INSERT INTO numbers (number) VALUES (?)", (number,))
            added += 1

    db.commit()
    await m.answer(f"✅ Добавлено номеров: {added}")


@dp.message(Command("list"))
async def list_numbers(m: Message):
    if not admin(m.from_user.id) or m.chat.type != "private":
        return

    rows = db.execute(
        "SELECT id, number FROM numbers WHERE used = 0 ORDER BY id LIMIT 50"
    ).fetchall()

    await m.answer(
        "\n".join(f"{i}. {n}" for i, n in rows)
        if rows else "Свободных номеров нет."
    )


@dp.message(Command("delete"))
async def delete_number(m: Message):
    if not admin(m.from_user.id) or m.chat.type != "private":
        return

    parts = m.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].isdigit():
        await m.answer("Используй: /delete ID")
        return

    cur = db.execute("DELETE FROM numbers WHERE id = ?", (int(parts[1]),))
    db.commit()
    await m.answer("✅ Удалено." if cur.rowcount else "Номер не найден.")


@dp.message(F.text)
async def auto_reply(m: Message):
    if m.chat.type == "private" or (m.from_user and m.from_user.is_bot):
        return

    if not re.search(r"\b(сл[её]т|номер)\b", m.text.lower()):
        return

    chat = db.execute(
        "SELECT enabled FROM chats WHERE chat_id = ?", (m.chat.id,)
    ).fetchone()

    if not chat or not chat[0]:
        return

    async with lock:
        row = db.execute(
            "SELECT id, number FROM numbers WHERE used = 0 ORDER BY id LIMIT 1"
        ).fetchone()

        if not row:
            await m.reply("Свободные номера закончились.")
            return

        db.execute("UPDATE numbers SET used = 1 WHERE id = ?", (row[0],))
        db.commit()

        try:
            await m.reply(f"📱 Номер: {row[1]}")
        except Exception:
            db.execute("UPDATE numbers SET used = 0 WHERE id = ?", (row[0],))
            db.commit()
            raise


async def main():
    print("Number Bot 2.0 запущен!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        db.close()
