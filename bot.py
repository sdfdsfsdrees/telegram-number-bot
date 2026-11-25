
import os
import re
import asyncio
import sqlite3

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

if not TOKEN or not ADMIN_ID:
    raise RuntimeError("Проверь BOT_TOKEN и ADMIN_ID в .env")

bot = Bot(TOKEN)
dp = Dispatcher()
db = sqlite3.connect("numbers.db", check_same_thread=False)
db.execute("PRAGMA journal_mode=WAL")
db.execute("PRAGMA busy_timeout=5000")
lock = asyncio.Lock()
pending = {}

# База данных
db.execute("""
CREATE TABLE IF NOT EXISTS numbers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    number TEXT NOT NULL,
    used INTEGER DEFAULT 0
)""")

db.execute("""
CREATE TABLE IF NOT EXISTS chats (
    chat_id INTEGER PRIMARY KEY,
    title TEXT DEFAULT '',
    enabled INTEGER DEFAULT 1
)""")

# Обновление старой базы
columns = [x[1] for x in db.execute("PRAGMA table_info(chats)")]
if "title" not in columns:
    db.execute("ALTER TABLE chats ADD COLUMN title TEXT DEFAULT ''")

columns = [x[1] for x in db.execute("PRAGMA table_info(numbers)")]
if "used" not in columns:
    db.execute("ALTER TABLE numbers ADD COLUMN used INTEGER DEFAULT 0")

db.execute("""
CREATE TABLE IF NOT EXISTS keywords (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    word TEXT UNIQUE COLLATE NOCASE
)""")

db.execute("""
CREATE TABLE IF NOT EXISTS settings (
    name TEXT PRIMARY KEY,
    value TEXT
)""")

db.execute("INSERT OR IGNORE INTO settings VALUES ('enabled', '1')")

for word in ("слет", "слёт", "номер"):
    db.execute("INSERT OR IGNORE INTO keywords (word) VALUES (?)", (word,))

db.execute("CREATE INDEX IF NOT EXISTS idx_numbers_used ON numbers(used, id)")
db.commit()


def is_admin(user_id):
    return user_id == ADMIN_ID


def enabled():
    return db.execute(
        "SELECT value FROM settings WHERE name='enabled'"
    ).fetchone()[0] == "1"


def button(text, data):
    return InlineKeyboardButton(text=text, callback_data=data)


def menu():
    status = "🟢 Выключить выдачу" if enabled() else "🔴 Включить выдачу"
    return InlineKeyboardMarkup(inline_keyboard=[
        [button(status, "bot:toggle")],
        [button("💬 Чаты", "chats"), button("📱 Номера", "numbers")],
        [button("🔑 Ключевые слова", "keywords")],
        [button("📊 Статистика", "stats")]
    ])


def chat_menu():
    rows = db.execute(
        "SELECT chat_id, title, enabled FROM chats ORDER BY title"
    ).fetchall()

    keyboard = [
        [
            button(
                f"{'🟢' if active else '🔴'} {title or chat_id}",
                f"chat:{chat_id}"
            ),
            button("❌", f"remove:{chat_id}")
        ]
        for chat_id, title, active in rows
    ]
    keyboard.append([button("⬅️ В меню", "home")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def keyword_menu():
    rows = db.execute(
        "SELECT id, word FROM keywords ORDER BY id"
    ).fetchall()

    keyboard = [
        [button(f"❌ {word}", f"kwdel:{kid}")]
        for kid, word in rows
    ]
    keyboard += [
        [button("➕ Добавить слово", "kwadd")],
        [button("⬅️ В меню", "home")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


@dp.message(Command("start", "panel"))
async def start(m: Message):
    if is_admin(m.from_user.id):
        await m.answer("🤖 NUMBER BOT 3.0\n\nПанель управления:", reply_markup=menu())
    else:
        await m.answer("Бот работает.")


@dp.callback_query()
async def callbacks(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True)
        return

    data = c.data

    if data == "home":
        await c.message.edit_text(
            "🤖 NUMBER BOT 3.0\n\nПанель управления:",
            reply_markup=menu()
        )

    elif data == "bot:toggle":
        db.execute(
            "UPDATE settings SET value=? WHERE name='enabled'",
            ("0" if enabled() else "1",)
        )
        db.commit()
        await c.message.edit_reply_markup(reply_markup=menu())

    elif data == "chats":
        await c.message.answer(
            "💬 Подключённые чаты\n\nНажми на чат, чтобы включить или выключить его:",
            reply_markup=chat_menu()
        )

    elif data.startswith("chat:"):
        cid = int(data.split(":")[1])
        db.execute(
            "UPDATE chats SET enabled=1-enabled WHERE chat_id=?", (cid,)
        )
        db.commit()
        await c.message.edit_reply_markup(reply_markup=chat_menu())

    elif data.startswith("remove:"):
        cid = int(data.split(":")[1])
        db.execute("DELETE FROM chats WHERE chat_id=?", (cid,))
        db.commit()
        await c.message.edit_reply_markup(reply_markup=chat_menu())

    elif data == "numbers":
        total, free = db.execute(
            "SELECT COUNT(*), COALESCE(SUM(used=0), 0) FROM numbers"
        ).fetchone()

        await c.message.answer(
            f"📱 НОМЕРА\n\nВсего: {total}\n"
            f"Свободно: {free}\nВыдано: {total-free}\n\n"
            "Добавить один: /add номер\n"
            "Добавить список: /addmany\n"
            "Посмотреть свободные: /list\n"
            "Удалить по ID: /delete ID"
        )

    elif data == "keywords":
        await c.message.answer(
            "🔑 КЛЮЧЕВЫЕ СЛОВА\n\n"
            "Нажми на слово, чтобы удалить его.",
            reply_markup=keyword_menu()
        )

    elif data == "kwadd":
        pending[c.from_user.id] = "keyword"
        await c.message.answer(
            "✏️ Отправь новое ключевое слово или фразу.\n"
            "Для отмены напиши /cancel."
        )

    elif data.startswith("kwdel:"):
        kid = int(data.split(":")[1])
        db.execute("DELETE FROM keywords WHERE id=?", (kid,))
        db.commit()
        await c.message.edit_reply_markup(reply_markup=keyword_menu())

    elif data == "stats":
        total, free = db.execute(
            "SELECT COUNT(*), COALESCE(SUM(used=0), 0) FROM numbers"
        ).fetchone()
        chats = db.execute(
            "SELECT COUNT(*) FROM chats WHERE enabled=1"
        ).fetchone()[0]
        words = db.execute("SELECT COUNT(*) FROM keywords").fetchone()[0]

        await c.message.answer(
            f"📊 СТАТИСТИКА\n\n"
            f"Состояние: {'Включён' if enabled() else 'Выключен'}\n"
            f"Всего номеров: {total}\n"
            f"Свободно: {free}\n"
            f"Выдано: {total-free}\n"
            f"Активных чатов: {chats}\n"
            f"Ключевых слов: {words}"
        )

    await c.answer()


@dp.message(Command("cancel"))
async def cancel(m: Message):
    pending.pop(m.from_user.id, None)
    await m.answer("Действие отменено.")


@dp.message(Command("addchat"))
async def add_chat(m: Message):
    if not is_admin(m.from_user.id):
        return

    if m.chat.type == "private":
        await m.answer("Добавь бота в группу и отправь там /addchat.")
        return

    db.execute("""
        INSERT INTO chats (chat_id, title, enabled)
        VALUES (?, ?, 1)
        ON CONFLICT(chat_id) DO UPDATE SET
        title=excluded.title, enabled=1
    """, (m.chat.id, m.chat.title or "Группа"))
    db.commit()
    await m.answer("✅ Чат подключён!")


# Добавление ключевого слова из меню
@dp.message(
    F.text,
    lambda m: m.chat.type == "private"
    and bool(m.from_user)
    and pending.get(m.from_user.id) == "keyword"
)
async def text_handler(m: Message):
    if m.from_user and is_admin(m.from_user.id):
        if m.chat.type == "private" and pending.get(m.from_user.id) == "keyword":
            word = m.text.strip()
            pending.pop(m.from_user.id, None)

            if not word or len(word) > 60:
                await m.answer("Слово пустое или слишком длинное.")
                return

            try:
                db.execute("INSERT INTO keywords (word) VALUES (?)", (word,))
                db.commit()
                await m.answer(f"✅ Добавлено: {word}")
            except sqlite3.IntegrityError:
                await m.answer("Такое слово уже есть.")
            return


@dp.message(Command("add"))
async def add_number(m: Message):
    if not is_admin(m.from_user.id) or m.chat.type != "private":
        return

    parts = m.text.split(maxsplit=1)
    if len(parts) < 2:
        await m.answer("Используй: /add +79990000000")
        return

    number = parts[1].strip()
    db.execute("INSERT INTO numbers (number) VALUES (?)", (number,))
    db.commit()
    await m.answer("✅ Номер добавлен!")


@dp.message(Command("addmany"))
async def add_many(m: Message):
    if not is_admin(m.from_user.id) or m.chat.type != "private":
        return

    lines = m.text.splitlines()[1:]
    added = 0

    for number in dict.fromkeys(x.strip() for x in lines if x.strip()):
        exists = db.execute(
            "SELECT 1 FROM numbers WHERE number=?", (number,)
        ).fetchone()
        if not exists:
            db.execute("INSERT INTO numbers (number) VALUES (?)", (number,))
            added += 1

    db.commit()
    await m.answer(f"✅ Добавлено номеров: {added}")


@dp.message(Command("list"))
async def list_numbers(m: Message):
    if not is_admin(m.from_user.id) or m.chat.type != "private":
        return

    rows = db.execute(
        "SELECT id, number FROM numbers WHERE used=0 ORDER BY id LIMIT 50"
    ).fetchall()

    await m.answer(
        "\n".join(f"{i}. {n}" for i, n in rows)
        if rows else "Свободных номеров нет."
    )


@dp.message(Command("delete"))
async def delete_number(m: Message):
    if not is_admin(m.from_user.id) or m.chat.type != "private":
        return

    parts = m.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].isdigit():
        await m.answer("Используй: /delete ID")
        return

    cur = db.execute("DELETE FROM numbers WHERE id=?", (int(parts[1]),))
    db.commit()
    await m.answer("✅ Удалено." if cur.rowcount else "Номер не найден.")


# Автоматическая выдача
@dp.message(F.text)
async def auto_reply(m: Message):
    if m.chat.type == "private" or (m.from_user and m.from_user.is_bot):
        return

    if not enabled():
        return

    chat = db.execute(
        "SELECT enabled FROM chats WHERE chat_id=?", (m.chat.id,)
    ).fetchone()

    if not chat or not chat[0]:
        return

    words = db.execute("SELECT word FROM keywords").fetchall()
    text = m.text.casefold()

    matched = any(
        re.search(rf"(?<!\w){re.escape(word.casefold())}(?!\w)", text)
        for (word,) in words
    )

    if not matched:
        return

    async with lock:
        row = db.execute("""
            SELECT id, number FROM numbers
            WHERE used=0 ORDER BY id LIMIT 1
        """).fetchone()

        if not row:
            await m.reply("Свободные номера закончились.")
            return

        db.execute("UPDATE numbers SET used=1 WHERE id=?", (row[0],))
        db.commit()

        try:
            await m.reply(f"📱 Номер: {row[1]}")
        except Exception:
            db.execute("UPDATE numbers SET used=0 WHERE id=?", (row[0],))
            db.commit()
            raise


async def main():
    print("NUMBER BOT 3.0 запущен!")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
