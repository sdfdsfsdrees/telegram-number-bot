import os
import re
import asyncio
import sqlite3

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BotCommand
)
from aiogram.exceptions import TelegramBadRequest

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

if not TOKEN or not ADMIN_ID:
    raise RuntimeError("Проверь BOT_TOKEN и ADMIN_ID в файле .env")

bot = Bot(token=TOKEN)
dp = Dispatcher()

db = sqlite3.connect("numbers.db", check_same_thread=False)
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
    enabled INTEGER DEFAULT 1,
    title TEXT DEFAULT ''
)
""")

# Обновление старой базы данных
try:
    db.execute("ALTER TABLE chats ADD COLUMN title TEXT DEFAULT ''")
except sqlite3.OperationalError:
    pass

db.commit()

number_lock = asyncio.Lock()

KEYWORDS = [
    "слет",
    "номер",
    "дай номер",
    "нужен номер"
]


def is_admin(message):
    return (
        message.from_user is not None
        and message.from_user.id == ADMIN_ID
    )


def admin_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="💬 Мои чаты",
                callback_data="chats"
            ),
            InlineKeyboardButton(
                text="📱 Номера",
                callback_data="numbers"
            )
        ],
        [
            InlineKeyboardButton(
                text="➕ Как добавить чат",
                callback_data="addchat_help"
            ),
            InlineKeyboardButton(
                text="📊 Статистика",
                callback_data="stats"
            )
        ]
    ])


@dp.message(Command("start"))
async def start(message: Message):
    if is_admin(message):
        await message.answer(
            "🤖 Number Bot 2.0\n\n"
            "Панель управления готова.",
            reply_markup=admin_keyboard()
        )
    else:
        await message.answer("Бот работает.")


@dp.message(Command("panel"))
async def panel(message: Message):
    if not is_admin(message):
        return

    await message.answer(
        "⚙️ Панель управления",
        reply_markup=admin_keyboard()
    )


@dp.callback_query(F.data == "addchat_help")
async def addchat_help(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа", show_alert=True)
        return

    await callback.message.answer(
        "💬 Подключение группы:\n\n"
        "1. Добавь бота в нужную группу.\n"
        "2. Отправь в группе команду /addchat.\n"
        "3. Группа появится в панели управления.\n\n"
        "Бот должен иметь доступ к сообщениям группы."
    )
    await callback.answer()


@dp.message(Command("addchat"))
async def add_chat(message: Message):
    if not is_admin(message):
        return

    if message.chat.type == "private":
        args = message.text.split(maxsplit=1)

        if len(args) < 2:
            await message.answer(
                "Чтобы подключить группу, добавь бота "
                "в неё и отправь там /addchat.\n\n"
                "Для публичной группы можно попробовать:\n"
                "/addchat @username"
            )
            return

        target = args[1].strip()

        # Принимаем публичные имена и ссылки t.me
        if "t.me/" in target:
            target = target.split("t.me/", 1)[1].strip("/")
            target = target.split("?", 1)[0]

        if target.startswith("@") or re.fullmatch(
            r"[A-Za-z0-9_]{5,}", target
        ):
            if not target.startswith("@"):
                target = "@" + target
        else:
            await message.answer(
                "Для закрытой группы используй /addchat "
                "непосредственно внутри группы."
            )
            return

        try:
            chat = await bot.get_chat(target)
            member = await bot.get_chat_member(
                chat.id, bot.id
            )

            if member.status in ("left", "kicked"):
                await message.answer(
                    "Сначала добавь бота в эту группу."
                )
                return

        except TelegramBadRequest:
            await message.answer(
                "Не удалось найти группу или проверить доступ. "
                "Добавь бота в группу и отправь /addchat внутри неё."
            )
            return

    else:
        chat = message.chat

    db.execute(
        """
        INSERT INTO chats (chat_id, enabled, title)
        VALUES (?, 1, ?)
        ON CONFLICT(chat_id) DO UPDATE SET
        enabled = 1,
        title = excluded.title
        """,
        (chat.id, chat.title or "Группа")
    )
    db.commit()

    await message.answer(
        f"✅ Чат подключён!\n"
        f"Название: {chat.title or 'Группа'}\n"
        f"ID: {chat.id}"
    )


@dp.callback_query(F.data == "chats")
async def show_chats(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа", show_alert=True)
        return

    rows = db.execute(
        "SELECT chat_id, enabled, title FROM chats"
    ).fetchall()

    if not rows:
        await callback.message.answer(
            "Подключённых чатов пока нет."
        )
        await callback.answer()
        return

    buttons = []

    for chat_id, enabled, title in rows:
        status = "🟢" if enabled else "🔴"

        buttons.append([
            InlineKeyboardButton(
                text=f"{status} {title or chat_id}",
                callback_data=f"info:{chat_id}"
            ),
            InlineKeyboardButton(
                text="Вкл/Выкл",
                callback_data=f"toggle:{chat_id}"
            ),
            InlineKeyboardButton(
                text="❌",
                callback_data=f"remove:{chat_id}"
            )
        ])

    await callback.message.answer(
        "💬 Подключённые чаты:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=buttons
        )
        "Посмотреть: /list\n"
        "Удалить: /delete ID"
    )
    await callback.answer()


@dp.callback_query(F.data == "stats")
async def show_stats(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа", show_alert=True)
        return

    total = db.execute(
        "SELECT COUNT(*) FROM numbers"
    ).fetchone()[0]

    free = db.execute(
        "SELECT COUNT(*) FROM numbers WHERE used = 0"
    ).fetchone()[0]

    chats = db.execute(
        "SELECT COUNT(*) FROM chats WHERE enabled = 1"
    ).fetchone()[0]

    await callback.message.answer(
        "📊 Статистика\n\n"
        f"Всего номеров: {total}\n"
        f"Свободно: {free}\n"
        f"Выдано: {total - free}\n"
        f"Активных чатов: {chats}"
    )
    await callback.answer()


@dp.message(Command("add"))
async def add_number(message: Message):
    if not is_admin(message):
        return

    parts = message.text.split(maxsplit=1)

    if len(parts) < 2:
        await message.answer(
            "Используй: /add +79990000000"
        )
        return

    number = parts[1].strip()

    db.execute(
        "INSERT INTO numbers (number) VALUES (?)",
        (number,)
    )
    db.commit()

    await message.answer("✅ Номер добавлен!")


@dp.message(Command("addmany"))
async def add_many(message: Message):
    if not is_admin(message):
        return

    parts = message.text.split(maxsplit=1)

    if len(parts) < 2:
        await message.answer(
            "Отправь номера через новую строку после /addmany"
        )
        return

    values = [
        line.strip()
        for line in parts[1].splitlines()
        if line.strip()
    ]

    added = 0

    for number in values:
        exists = db.execute(
            "SELECT id FROM numbers WHERE number = ?",
            (number,)
        ).fetchone()

        if not exists:
            db.execute(
                "INSERT INTO numbers (number) VALUES (?)",
                (number,)
            )
            added += 1

    db.commit()

    await message.answer(
        f"✅ Добавлено: {added}\n"
        f"Пропущено дубликатов: {len(values) - added}"
    )


@dp.message(Command("list"))
async def list_numbers(message: Message):
    if not is_admin(message):
        return

    rows = db.execute(
        "SELECT id, number FROM numbers WHERE used = 0 "
        "ORDER BY id LIMIT 50"
    ).fetchall()

    if not rows:
        await message.answer("Свободных номеров нет.")
        return

    result = "\n".join(
        f"{row[0]}. {row[1]}" for row in rows
    )

    await message.answer("📱 Свободные номера:\n\n" + result)


@dp.message(Command("delete"))
async def delete_number(message: Message):
    if not is_admin(message):
        return

    parts = message.text.split(maxsplit=1)

    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Используй: /delete ID")
        return

    number_id = int(parts[1])

    cursor = db.execute(
        "DELETE FROM numbers WHERE id = ?",
        (number_id,)
    )
    db.commit()

    await message.answer(
        "✅ Удалено." if cursor.rowcount else "Номер не найден."
    )


@dp.message(Command("chats"))
async def chats_command(message: Message):
    if not is_admin(message):
        return

    rows = db.execute(
        "SELECT chat_id, enabled, title FROM chats"
    ).fetchall()

    if not rows:
        await message.answer("Подключённых чатов нет.")
        return

    result = "\n".join(
        f"{title or chat_id} | ID: {chat_id} | "
        f"{'Включён' if enabled else 'Выключен'}"
        for chat_id, enabled, title in rows
    )

    await message.answer(result)


@dp.message(Command("removechat"))
async def remove_chat_command(message: Message):
    if not is_admin(message):
        return

    parts = message.text.split(maxsplit=1)

    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        await message.answer("Используй: /removechat ID")
        return
        chat_id = int(parts[1])

    db.execute(
        "DELETE FROM chats WHERE chat_id = ?",
        (chat_id,)
    )
    db.commit()

    await message.answer("✅ Чат удалён из настроек.")


@dp.message(F.text)
async def handle_group_message(message: Message):
    if message.chat.type == "private":
        return

    if message.from_user and message.from_user.is_bot:
        return

    text = message.text.lower().replace("ё", "е")

    if text.startswith("/"):
        return

    pattern = r"(?<!\w)(?:слет|номер|дай\s+номер|нужен\s+номер)(?!\w)"

    if not re.search(pattern, text):
        return

    chat = db.execute(
        "SELECT enabled FROM chats WHERE chat_id = ?",
        (message.chat.id,)
    ).fetchone()

    if not chat or not chat[0]:
        return

    async with number_lock:
        row = db.execute(
            "SELECT id, number FROM numbers "
            "WHERE used = 0 ORDER BY id LIMIT 1"
        ).fetchone()

        if not row:
            await message.reply("Свободные номера закончились.")
            return

        number_id, number = row

        db.execute(
            "UPDATE numbers SET used = 1 WHERE id = ?",
            (number_id,)
        )
        db.commit()

        try:
            await message.reply(f"📱 Номер: {number}")
        except Exception:
            db.execute(
                "UPDATE numbers SET used = 0 WHERE id = ?",
                (number_id,)
            )
            db.commit()
            raise


async def main():
    await bot.set_my_commands([
        BotCommand(command="start", description="Запуск"),
        BotCommand(command="panel", description="Панель управления"),
        BotCommand(command="add", description="Добавить номер"),
        BotCommand(command="addmany", description="Добавить список номеров"),
        BotCommand(command="list", description="Свободные номера"),
        BotCommand(command="delete", description="Удалить номер"),
        BotCommand(command="addchat", description="Подключить чат"),
        BotCommand(command="chats", description="Список чатов"),
        BotCommand(command="removechat", description="Удалить чат")
    ])

    print("Number Bot 2.0 запущен!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        db.close()
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("toggle:"))
async def toggle_chat(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа", show_alert=True)
        return

    chat_id = int(callback.data.split(":")[1])

    db.execute(
        """
        UPDATE chats
        SET enabled = CASE WHEN enabled = 1 THEN 0 ELSE 1 END
        WHERE chat_id = ?
        """,
        (chat_id,)
    )
    db.commit()

    await callback.answer("Настройки чата изменены")
    await show_chats(callback)


@dp.callback_query(F.data.startswith("remove:"))
async def remove_chat(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа", show_alert=True)
        return

    chat_id = int(callback.data.split(":")[1])

    db.execute(
        "DELETE FROM chats WHERE chat_id = ?",
        (chat_id,)
    )
    db.commit()

    await callback.answer("Чат удалён")
    await show_chats(callback)


@dp.callback_query(F.data == "numbers")
async def show_numbers(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа", show_alert=True)
        return

    total = db.execute(
        "SELECT COUNT(*) FROM numbers"
    ).fetchone()[0]

    free = db.execute(
        "SELECT COUNT(*) FROM numbers WHERE used = 0"
    ).fetchone()[0]

    await callback.message.answer(
        f"📱 Номера\n\n"
        f"Всего: {total}\n"
        f"Свободно: {free}\n"
        f"Выдано: {total - free}\n\n"
        "Добавить: /add номер\n"
        "Добавить списком: /addmany\n"
