import asyncio
import logging
import os
import re
import sqlite3
import time

from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from dotenv import load_dotenv

try:  # быстрый цикл событий (Linux/macOS); на Windows просто пропускается
    import uvloop
    uvloop.install()
except ImportError:
    pass

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DB_PATH = os.getenv("DB_PATH", "numbers.db")
# Пауза между выдачами одному человеку в одном чате (секунды). 0 = без паузы.
USER_COOLDOWN = int(os.getenv("USER_COOLDOWN", "30"))
# Как часто писать в чат «номера закончились» (секунды).
EMPTY_NOTICE_COOLDOWN = 300
# Если в сообщении больше стольких слов — это «длинное предложение», номер не выдаём.
MAX_WORDS = int(os.getenv("MAX_WORDS", "4"))
# Фразы-исключения: по ним номер выдаётся даже в длинном сообщении.
ALLOW_PHRASES = ("дайте номер", "дай номер", "дайте номерок", "дай номерок")

if not TOKEN or not ADMIN_ID:
    raise RuntimeError("Проверь BOT_TOKEN и ADMIN_ID в .env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("number_bot")

# Держим соединение с Telegram живым: иначе после пары секунд простоя
# следующий ответ тратит 100-300 мс на новое TCP+TLS-соединение.
session = AiohttpSession()
if hasattr(session, "_connector_init"):
    session._connector_init["keepalive_timeout"] = 120
bot = Bot(TOKEN, session=session)
dp = Dispatcher()

db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.execute("PRAGMA journal_mode=WAL")
db.execute("PRAGMA busy_timeout=5000")
db.execute("PRAGMA synchronous=NORMAL")   # в WAL безопасно, но без fsync на каждый commit
db.execute("PRAGMA temp_store=MEMORY")
db.execute("PRAGMA cache_size=-16000")

pending = {}          # user_id -> ожидаемое действие (например "keyword")
last_issue = {}       # (chat_id, user_id) -> время последней выдачи
empty_notice = {}     # chat_id -> время последнего сообщения «закончились»
state = {"admin_notified": False, "kw_regex": None, "enabled": True}
active_chats = set()  # chat_id включённых чатов — в памяти, без запросов в базу


# ---------------------------------------------------------------- База данных

def add_column(table, column, ddl):
    cols = [r[1] for r in db.execute(f"PRAGMA table_info({table})")]
    if column not in cols:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


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

# Миграции старой базы + новые колонки для истории выдачи
add_column("chats", "title", "TEXT DEFAULT ''")
add_column("numbers", "used", "INTEGER DEFAULT 0")
add_column("numbers", "used_at", "INTEGER")
add_column("numbers", "used_by", "INTEGER")
add_column("numbers", "used_by_name", "TEXT")
add_column("numbers", "used_chat", "INTEGER")

db.execute("INSERT OR IGNORE INTO settings VALUES ('enabled', '1')")
for _w in ("слет", "номер"):
    db.execute("INSERT OR IGNORE INTO keywords (word) VALUES (?)", (_w,))
db.execute("CREATE INDEX IF NOT EXISTS idx_numbers_used ON numbers(used, id)")
db.commit()


# ------------------------------------------------------------------- Помощники

def is_admin(user_id):
    return user_id == ADMIN_ID


def enabled():
    return state["enabled"]


def reload_enabled():
    state["enabled"] = db.execute(
        "SELECT value FROM settings WHERE name='enabled'"
    ).fetchone()[0] == "1"


def reload_chats():
    active_chats.clear()
    active_chats.update(
        r[0] for r in db.execute("SELECT chat_id FROM chats WHERE enabled=1")
    )


def norm(text):
    """Нижний регистр + ё -> е, чтобы «слёт» и «слет» были одним словом."""
    return text.casefold().replace("ё", "е")


ALLOW_REGEX = re.compile(
    r"(?<!\w)(?:"
    + "|".join(re.escape(norm(p)).replace("\\ ", r"\s+") for p in ALLOW_PHRASES)
    + r")(?!\w)"
)


def reload_keywords():
    """Один раз собираем регулярку, а не лезем в базу на каждое сообщение."""
    words = [r[0] for r in db.execute("SELECT word FROM keywords")]
    if not words:
        state["kw_regex"] = None
        return
    parts = sorted({re.escape(norm(w)) for w in words}, key=len, reverse=True)
    state["kw_regex"] = re.compile(r"(?<!\w)(?:" + "|".join(parts) + r")(?!\w)")


def number_counts():
    total, free = db.execute(
        "SELECT COUNT(*), COALESCE(SUM(used=0), 0) FROM numbers"
    ).fetchone()
    return total, free


def numbers_added():
    """После пополнения базы снова разрешаем уведомить админа о конце номеров."""
    state["admin_notified"] = False


def short(text, limit=30):
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def is_admin_private(m: Message):
    return bool(m.from_user) and is_admin(m.from_user.id) and m.chat.type == "private"


async def safe_edit(c: CallbackQuery, text=None, markup=None):
    """Не падаем, если Telegram говорит «message is not modified»."""
    try:
        if text is None:
            await c.message.edit_reply_markup(reply_markup=markup)
        else:
            await c.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


# ------------------------------------------------------------------- Клавиатуры

def button(text, data):
    return InlineKeyboardButton(text=text, callback_data=data)


def menu():
    status = "🟢 Выключить выдачу" if enabled() else "🔴 Включить выдачу"
    return InlineKeyboardMarkup(inline_keyboard=[
        [button(status, "bot:toggle")],
        [button("💬 Чаты", "chats"), button("📱 Номера", "numbers")],
        [button("🔑 Ключевые слова", "keywords")],
        [button("📊 Статистика", "stats")],
    ])


def chat_menu():
    rows = db.execute(
        "SELECT chat_id, title, enabled FROM chats ORDER BY title"
    ).fetchall()
    keyboard = [
        [
            button(
                f"{'🟢' if active else '🔴'} {short(title or chat_id)}",
                f"chat:{chat_id}",
            ),
            button("❌", f"remove:{chat_id}"),
        ]
        for chat_id, title, active in rows
    ]
    keyboard.append([button("⬅️ В меню", "home")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def keyword_menu():
    rows = db.execute("SELECT id, word FROM keywords ORDER BY id").fetchall()
    keyboard = [[button(f"❌ {short(word)}", f"kwdel:{kid}")] for kid, word in rows]
    keyboard += [
        [button("➕ Добавить слово", "kwadd")],
        [button("⬅️ В меню", "home")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


PANEL_TEXT = "🤖 NUMBER BOT 3.1\n\nПанель управления:"


# --------------------------------------------------------------------- Команды

@dp.message(Command("start", "panel"))
async def start(m: Message):
    if m.chat.type != "private":
        return
    if m.from_user and is_admin(m.from_user.id):
        await m.answer(PANEL_TEXT, reply_markup=menu())
    else:
        await m.answer("Бот работает.")


@dp.message(Command("cancel"))
async def cancel(m: Message):
    if m.from_user:
        pending.pop(m.from_user.id, None)
    await m.answer("Действие отменено.")


@dp.message(Command("addchat"))
async def add_chat(m: Message):
    if not m.from_user or not is_admin(m.from_user.id):
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
    reload_chats()
    await m.answer("✅ Чат подключён!")


@dp.message(Command("add"))
async def add_number(m: Message):
    if not is_admin_private(m):
        return

    parts = m.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await m.answer("Используй: /add +79990000000")
        return

    number = parts[1].strip()
    if db.execute("SELECT 1 FROM numbers WHERE number=?", (number,)).fetchone():
        await m.answer("Такой номер уже есть.")
        return

    db.execute("INSERT INTO numbers (number) VALUES (?)", (number,))
    db.commit()
    numbers_added()
    await m.answer("✅ Номер добавлен!")


@dp.message(Command("addmany"))
async def add_many(m: Message):
    if not is_admin_private(m):
        return

    # Номера можно писать и в первой строке рядом с командой, и ниже
    parts = m.text.split(maxsplit=1)
    raw = parts[1] if len(parts) > 1 else ""
    numbers = list(dict.fromkeys(x.strip() for x in raw.splitlines() if x.strip()))

    if not numbers:
        await m.answer("Используй:\n/addmany\n+79990000001\n+79990000002")
        return

    added = 0
    for number in numbers:
        if not db.execute(
            "SELECT 1 FROM numbers WHERE number=?", (number,)
        ).fetchone():
            db.execute("INSERT INTO numbers (number) VALUES (?)", (number,))
            added += 1

    db.commit()
    if added:
        numbers_added()
    await m.answer(
        f"✅ Добавлено номеров: {added}\n"
        f"Пропущено (уже были): {len(numbers) - added}"
    )


@dp.message(Command("list"))
async def list_numbers(m: Message):
    if not is_admin_private(m):
        return

    rows = db.execute(
        "SELECT id, number FROM numbers WHERE used=0 ORDER BY id LIMIT 50"
    ).fetchall()
    _, free = number_counts()

    if not rows:
        await m.answer("Свободных номеров нет.")
        return

    text = "\n".join(f"{i}. {n}" for i, n in rows)
    if free > len(rows):
        text += f"\n\n…и ещё {free - len(rows)}"
    await m.answer(text)


@dp.message(Command("history"))
async def history(m: Message):
    """Последние 20 выданных номеров: кому, где и когда."""
    if not is_admin_private(m):
        return

    rows = db.execute("""
        SELECT n.id, n.number, n.used_at, n.used_by_name,
               COALESCE(c.title, n.used_chat)
        FROM numbers n
        LEFT JOIN chats c ON c.chat_id = n.used_chat
        WHERE n.used=1
        ORDER BY n.used_at DESC, n.id DESC
        LIMIT 20
    """).fetchall()

    if not rows:
        await m.answer("Выдач пока не было.")
        return

    lines = []
    for nid, number, ts, who, chat in rows:
        when = time.strftime("%d.%m %H:%M", time.localtime(ts)) if ts else "—"
        lines.append(f"{nid}. {number} → {who or '—'} ({chat or '—'}, {when})")
    await m.answer("📜 Последние выдачи:\n\n" + "\n".join(lines))


@dp.message(Command("restore"))
async def restore_number(m: Message):
    """Вернуть выданный номер обратно в свободные."""
    if not is_admin_private(m):
        return

    parts = m.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().isdigit():
        await m.answer("Используй: /restore ID")
        return

    cur = db.execute("""
        UPDATE numbers
        SET used=0, used_at=NULL, used_by=NULL, used_by_name=NULL, used_chat=NULL
        WHERE id=? AND used=1
    """, (int(parts[1]),))
    db.commit()
    if cur.rowcount:
        numbers_added()
    await m.answer("✅ Номер снова свободен." if cur.rowcount else "Номер не найден или не выдан.")


@dp.message(Command("delete"))
async def delete_number(m: Message):
    if not is_admin_private(m):
        return

    parts = m.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().isdigit():
        await m.answer("Используй: /delete ID")
        return

    cur = db.execute("DELETE FROM numbers WHERE id=?", (int(parts[1]),))
    db.commit()
    await m.answer("✅ Удалено." if cur.rowcount else "Номер не найден.")


# -------------------------------------------------------------- Кнопки (меню)

@dp.callback_query()
async def callbacks(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True)
        return

    if c.message is None:
        await c.answer()
        return

    data = c.data

    if data == "home":
        await safe_edit(c, PANEL_TEXT, menu())

    elif data == "bot:toggle":
        db.execute(
            "UPDATE settings SET value=? WHERE name='enabled'",
            ("0" if enabled() else "1",),
        )
        db.commit()
        reload_enabled()
        await safe_edit(c, markup=menu())

    elif data == "chats":
        await c.message.answer(
            "💬 Подключённые чаты\n\n"
            "Нажми на чат, чтобы включить или выключить его.\n"
            "Новый чат: добавь бота в группу и отправь там /addchat",
            reply_markup=chat_menu(),
        )

    elif data.startswith("chat:"):
        cid = int(data.split(":")[1])
        db.execute("UPDATE chats SET enabled=1-enabled WHERE chat_id=?", (cid,))
        db.commit()
        reload_chats()
        await safe_edit(c, markup=chat_menu())

    elif data.startswith("remove:"):
        cid = int(data.split(":")[1])
        db.execute("DELETE FROM chats WHERE chat_id=?", (cid,))
        db.commit()
        reload_chats()
        await safe_edit(c, markup=chat_menu())

    elif data == "numbers":
        total, free = number_counts()
        await c.message.answer(
            f"📱 НОМЕРА\n\nВсего: {total}\n"
            f"Свободно: {free}\nВыдано: {total - free}\n\n"
            "Добавить один: /add номер\n"
            "Добавить список: /addmany (номера с новой строки)\n"
            "Свободные: /list\n"
            "История выдач: /history\n"
            "Вернуть номер в свободные: /restore ID\n"
            "Удалить по ID: /delete ID"
        )

    elif data == "keywords":
        await c.message.answer(
            "🔑 КЛЮЧЕВЫЕ СЛОВА\n\nНажми на слово, чтобы удалить его.",
            reply_markup=keyword_menu(),
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
        reload_keywords()
        await safe_edit(c, markup=keyword_menu())

    elif data == "stats":
        total, free = number_counts()
        chats = db.execute(
            "SELECT COUNT(*) FROM chats WHERE enabled=1"
        ).fetchone()[0]
        words = db.execute("SELECT COUNT(*) FROM keywords").fetchone()[0]

        await c.message.answer(
            f"📊 СТАТИСТИКА\n\n"
            f"Состояние: {'Включён' if enabled() else 'Выключен'}\n"
            f"Всего номеров: {total}\n"
            f"Свободно: {free}\n"
            f"Выдано: {total - free}\n"
            f"Активных чатов: {chats}\n"
            f"Ключевых слов: {words}"
        )

    await c.answer()


# ------------------------------------------------ Добавление ключевого слова

def awaiting_keyword(m: Message):
    return (
        m.chat.type == "private"
        and m.from_user is not None
        and is_admin(m.from_user.id)
        and pending.get(m.from_user.id) == "keyword"
        and not (m.text or "").startswith("/")  # команды не считаем словом
    )


@dp.message(F.text, awaiting_keyword)
async def keyword_input(m: Message):
    word = m.text.strip()
    pending.pop(m.from_user.id, None)

    if not word or len(word) > 60:
        await m.answer("Слово пустое или слишком длинное.")
        return

    try:
        db.execute("INSERT INTO keywords (word) VALUES (?)", (word,))
        db.commit()
    except sqlite3.IntegrityError:
        await m.answer("Такое слово уже есть.")
        return

    reload_keywords()
    await m.answer(f"✅ Добавлено: {word}")


# ---------------------------------------------------------- Автоматическая выдача

async def handle_empty(m: Message):
    """Номера закончились: не спамим чат и один раз предупреждаем админа."""
    now = time.monotonic()
    if now - empty_notice.get(m.chat.id, float("-inf")) > EMPTY_NOTICE_COOLDOWN:
        empty_notice[m.chat.id] = now
        try:
            await m.reply("Свободные номера закончились.")
        except Exception:
            log.exception("Не удалось отправить сообщение о конце номеров")

    if not state["admin_notified"]:
        state["admin_notified"] = True
        try:
            await bot.send_message(ADMIN_ID, "⚠️ Свободные номера закончились. Добавь новые: /addmany")
        except Exception:
            log.exception("Не удалось уведомить админа")


def purge_cooldowns(now):
    if len(last_issue) > 5000:
        for key in [k for k, t in last_issue.items() if now - t > USER_COOLDOWN]:
            del last_issue[key]


@dp.message(F.text)
async def auto_reply(m: Message):
    t0 = time.perf_counter()
    if m.chat.type == "private" or not m.from_user or m.from_user.is_bot:
        return

    # Сначала дешёвая проверка по тексту, потом уже база
    text = norm(m.text)
    regex = state["kw_regex"]
    if regex is None or not regex.search(text):
        return

    # Длинные предложения игнорируем, кроме фраз-исключений («дайте номер» и т.п.)
    if len(text.split()) > MAX_WORDS and not ALLOW_REGEX.search(text):
        return

    if not state["enabled"] or m.chat.id not in active_chats:
        return

    user_id = m.from_user.id
    now = time.monotonic()

    # Защита от спама: один человек не вычерпает все номера (админа не касается)
    if USER_COOLDOWN and not is_admin(user_id):
        last = last_issue.get((m.chat.id, user_id))
        if last is not None and now - last < USER_COOLDOWN:
            return

    # Между SELECT и UPDATE нет await, поэтому два сообщения не получат один номер,
    # а блокировка на время отправки больше не тормозит остальные ответы.
    row = db.execute(
        "SELECT id, number FROM numbers WHERE used=0 ORDER BY id LIMIT 1"
    ).fetchone()

    if not row:
        await handle_empty(m)
        return

    who = (
        f"@{m.from_user.username}" if m.from_user.username else m.from_user.full_name
    )
    db.execute(
        "UPDATE numbers SET used=1, used_at=?, used_by=?, used_by_name=?, used_chat=? "
        "WHERE id=?",
        (int(time.time()), user_id, who, m.chat.id, row[0]),
    )
    # commit сделаем уже после отправки: соединение с базой одно, поэтому
    # другие сообщения видят номер как занятый и дубля не будет.

    t1 = time.perf_counter()
    try:
        await m.reply(f"📱 Номер: {row[1]}")
        t2 = time.perf_counter()
    except Exception:
        # Не отправилось (сообщение удалили, нет прав и т.д.) — возвращаем номер
        log.exception("Ошибка отправки номера, возвращаю его в свободные")
        db.execute(
            "UPDATE numbers SET used=0, used_at=NULL, used_by=NULL, "
            "used_by_name=NULL, used_chat=NULL WHERE id=?",
            (row[0],),
        )
        db.commit()
        return

    db.commit()
    last_issue[(m.chat.id, user_id)] = now
    purge_cooldowns(now)
    log.info(
        "Номер %s выдан %s в чате %s | обработка %.1f мс, отправка в Telegram %.0f мс, "
        "сообщение дошло до бота за ~%d с",
        row[1], who, m.chat.id,
        (t1 - t0) * 1000, (t2 - t1) * 1000,
        max(0, int(time.time() - m.date.timestamp())),
    )


# ----------------------------------------------------------------------- Запуск

async def keep_warm():
    """Лёгкий пинг, чтобы соединение с Telegram не засыпало между выдачами."""
    while True:
        await asyncio.sleep(20)
        try:
            await bot.get_me()
        except Exception:
            pass


async def main():
    reload_keywords()
    reload_enabled()
    reload_chats()
    log.info("NUMBER BOT 3.1 запущен!")
    warm = asyncio.create_task(keep_warm())
    try:
        await dp.start_polling(
            bot,
            allowed_updates=["message", "callback_query"],
            polling_timeout=30,
        )
    finally:
        warm.cancel()
        await bot.session.close()
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
