import asyncio
import html
import logging
import math
import os
import re
import sqlite3
import time

from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
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

VERSION = "4.0"
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DB_PATH = os.getenv("DB_PATH", "numbers.db")
# Пауза между выдачами одному человеку в одном чате (секунды). 0 = без паузы.
USER_COOLDOWN = int(os.getenv("USER_COOLDOWN", "30"))
# Как часто писать в чат «номера закончились» (секунды).
EMPTY_NOTICE_COOLDOWN = 300
# Если в сообщении больше стольких слов — это «длинное предложение», номер не выдаём.
MAX_WORDS = int(os.getenv("MAX_WORDS", "4"))
# Когда свободных номеров столько или меньше — в уведомлении будет предупреждение.
LOW_STOCK = int(os.getenv("LOW_STOCK", "5"))
# Фразы-исключения: по ним номер выдаётся даже в длинном сообщении.
ALLOW_PHRASES = ("дайте номер", "дай номер", "дайте номерок", "дай номерок")
PAGE_SIZE = 10

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

try:
    from aiogram.client.default import DefaultBotProperties
    bot = Bot(
        TOKEN,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
except ImportError:  # старый aiogram 3.0–3.6
    bot = Bot(TOKEN, session=session, parse_mode=ParseMode.HTML)
dp = Dispatcher()

db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.execute("PRAGMA journal_mode=WAL")
db.execute("PRAGMA busy_timeout=5000")
db.execute("PRAGMA synchronous=NORMAL")
db.execute("PRAGMA temp_store=MEMORY")
db.execute("PRAGMA cache_size=-16000")

pending = {}          # user_id -> ожидаемое действие ("keyword" / "numbers")
last_issue = {}       # (chat_id, user_id) -> время последней выдачи
empty_notice = {}     # chat_id -> время последнего сообщения «закончились»
state = {"admin_notified": False, "kw_regex": None, "enabled": True, "notify": True}
active_chats = set()  # chat_id включённых чатов — в памяти, без запросов в базу
background = set()    # ссылки на фоновые задачи, чтобы их не съел сборщик мусора


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

# Миграции старой базы + колонки для истории выдачи
add_column("chats", "title", "TEXT DEFAULT ''")
add_column("numbers", "used", "INTEGER DEFAULT 0")
add_column("numbers", "used_at", "INTEGER")
add_column("numbers", "used_by", "INTEGER")
add_column("numbers", "used_by_name", "TEXT")
add_column("numbers", "used_chat", "INTEGER")

db.execute("INSERT OR IGNORE INTO settings VALUES ('enabled', '1')")
db.execute("INSERT OR IGNORE INTO settings VALUES ('notify', '1')")
for _w in ("слет", "номер"):
    db.execute("INSERT OR IGNORE INTO keywords (word) VALUES (?)", (_w,))
db.execute("CREATE INDEX IF NOT EXISTS idx_numbers_used ON numbers(used, id)")
db.commit()


# ------------------------------------------------------------------- Помощники

def is_admin(user_id):
    return user_id == ADMIN_ID


def esc(value):
    return html.escape(str(value), quote=False)


def enabled():
    return state["enabled"]


def reload_settings():
    rows = dict(db.execute("SELECT name, value FROM settings"))
    state["enabled"] = rows.get("enabled", "1") == "1"
    state["notify"] = rows.get("notify", "1") == "1"


def set_setting(name, value):
    db.execute(
        "INSERT INTO settings (name, value) VALUES (?, ?) "
        "ON CONFLICT(name) DO UPDATE SET value=excluded.value",
        (name, value),
    )
    db.commit()
    reload_settings()


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


def bar(free, total, width=10):
    """Полоска остатка номеров: ▰▰▰▰▰▱▱▱▱▱"""
    filled = round(width * free / total) if total else 0
    return "▰" * filled + "▱" * (width - filled)


def stamp(ts):
    return time.strftime("%d.%m %H:%M", time.localtime(ts)) if ts else "—"


def add_numbers(numbers):
    """Добавляет список номеров, возвращает (добавлено, пропущено)."""
    numbers = list(dict.fromkeys(n.strip() for n in numbers if n.strip()))
    added = 0
    for number in numbers:
        if len(number) > 32:
            continue
        if not db.execute("SELECT 1 FROM numbers WHERE number=?", (number,)).fetchone():
            db.execute("INSERT INTO numbers (number) VALUES (?)", (number,))
            added += 1
    db.commit()
    if added:
        numbers_added()
    return added, len(numbers) - added


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


def message_link(m: Message):
    """Ссылка на конкретное сообщение в группе (None, если у чата её не бывает)."""
    chat = m.chat
    if chat.username:
        base = f"https://t.me/{chat.username}"
    elif str(chat.id).startswith("-100"):
        base = f"https://t.me/c/{str(chat.id)[4:]}"
    else:
        return None
    thread = m.message_thread_id if getattr(m, "is_topic_message", False) else None
    if thread:
        return f"{base}/{thread}/{m.message_id}"
    return f"{base}/{m.message_id}"


# ------------------------------------------------------------------- Клавиатуры

def button(text, data):
    return InlineKeyboardButton(text=text, callback_data=data)


def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


LINE = "━━━━━━━━━━━━━━━━━━"


# ------------------------------------------------------------------- Экраны

def panel_view():
    total, free = number_counts()
    chats = db.execute("SELECT COUNT(*) FROM chats WHERE enabled=1").fetchone()[0]
    words = db.execute("SELECT COUNT(*) FROM keywords").fetchone()[0]

    status = "🟢 <b>Выдача включена</b>" if enabled() else "🔴 <b>Выдача выключена</b>"
    notify = "включены" if state["notify"] else "выключены"
    text = (
        f"🤖 <b>NUMBER BOT</b>  <i>v{VERSION}</i>\n"
        f"{LINE}\n"
        f"{status}\n\n"
        f"📱 Номера: <b>{free}</b> свободно из <b>{total}</b>\n"
        f"<code>{bar(free, total)}</code>\n\n"
        f"💬 Активных чатов: <b>{chats}</b>\n"
        f"🔑 Ключевых слов: <b>{words}</b>\n"
        f"🔔 Уведомления: <b>{notify}</b>"
    )
    markup = kb([
        [button("🔴 Выключить выдачу" if enabled() else "🟢 Включить выдачу", "bot:toggle")],
        [button("📱 Номера", "numbers"), button("💬 Чаты", "chats")],
        [button("🔑 Слова", "keywords"), button("📊 Статистика", "stats")],
        [button("🔔 Уведомления: вкл" if state["notify"] else "🔕 Уведомления: выкл",
                "notify:toggle")],
    ])
    return text, markup


def numbers_view():
    total, free = number_counts()
    used = total - free
    text = (
        f"📱 <b>НОМЕРА</b>\n{LINE}\n"
        f"Свободно: <b>{free}</b>\n"
        f"Выдано: <b>{used}</b>\n"
        f"Всего: <b>{total}</b>\n"
        f"<code>{bar(free, total)}</code>\n\n"
        "<i>Командами:</i>\n"
        "<code>/add +79990000000</code> — один номер\n"
        "<code>/delete ID</code> — удалить\n"
        "<code>/restore ID</code> — вернуть выданный"
    )
    markup = kb([
        [button("➕ Добавить", "numadd"), button("📋 Свободные", "free:0")],
        [button("📜 История", "hist:0"), button("🧹 Очистить выданные", "numclear")],
        [button("⬅️ В меню", "home")],
    ])
    return text, markup


def pager(prefix, page, pages):
    row = []
    if page > 0:
        row.append(button("◀️", f"{prefix}:{page - 1}"))
    row.append(button(f"{page + 1}/{pages}", "noop"))
    if page < pages - 1:
        row.append(button("▶️", f"{prefix}:{page + 1}"))
    return row


def free_view(page=0):
    _, free = number_counts()
    pages = max(1, math.ceil(free / PAGE_SIZE))
    page = max(0, min(page, pages - 1))
    rows = db.execute(
        "SELECT id, number FROM numbers WHERE used=0 ORDER BY id LIMIT ? OFFSET ?",
        (PAGE_SIZE, page * PAGE_SIZE),
    ).fetchall()

    if rows:
        body = "\n".join(f"<code>{i}</code> · {esc(n)}" for i, n in rows)
    else:
        body = "<i>Свободных номеров нет.</i>"
    text = f"📋 <b>СВОБОДНЫЕ НОМЕРА</b>  ({free})\n{LINE}\n{body}"
    markup = kb([pager("free", page, pages), [button("⬅️ К номерам", "numbers")]])
    return text, markup


def history_view(page=0):
    total = db.execute("SELECT COUNT(*) FROM numbers WHERE used=1").fetchone()[0]
    per_page = 8
    pages = max(1, math.ceil(total / per_page))
    page = max(0, min(page, pages - 1))
    rows = db.execute("""
        SELECT n.id, n.number, n.used_at, n.used_by_name,
               COALESCE(c.title, n.used_chat)
        FROM numbers n
        LEFT JOIN chats c ON c.chat_id = n.used_chat
        WHERE n.used=1
        ORDER BY n.used_at DESC, n.id DESC
        LIMIT ? OFFSET ?
    """, (per_page, page * per_page)).fetchall()

    if rows:
        body = "\n\n".join(
            f"<code>{nid}</code> · <b>{esc(number)}</b>\n"
            f"    👤 {esc(who or '—')}  💬 {esc(short(chat or '—', 24))}\n"
            f"    🕒 {stamp(ts)}"
            for nid, number, ts, who, chat in rows
        )
    else:
        body = "<i>Выдач пока не было.</i>"

    keyboard = []
    restore = [button(f"↩️ {nid}", f"restore:{nid}:{page}") for nid, *_ in rows]
    for i in range(0, len(restore), 4):
        keyboard.append(restore[i:i + 4])
    keyboard.append(pager("hist", page, pages))
    keyboard.append([button("⬅️ К номерам", "numbers")])

    text = f"📜 <b>ИСТОРИЯ ВЫДАЧ</b>  ({total})\n{LINE}\n{body}"
    if rows:
        text += "\n\n<i>↩️ — вернуть номер в свободные</i>"
    return text, kb(keyboard)


def clear_view():
    used = db.execute("SELECT COUNT(*) FROM numbers WHERE used=1").fetchone()[0]
    text = (
        f"🧹 <b>Очистить выданные?</b>\n\n"
        f"Будет удалено номеров: <b>{used}</b>.\n"
        "История выдач пропадёт. Свободные номера не тронем."
    )
    markup = kb([
        [button("✅ Да, удалить", "numclear:yes"), button("✖️ Отмена", "numbers")],
    ])
    return text, markup


def chats_view():
    rows = db.execute(
        "SELECT chat_id, title, enabled FROM chats ORDER BY title"
    ).fetchall()
    keyboard = [
        [
            button(
                f"{'🟢' if active else '🔴'} {short(title or chat_id)}",
                f"chat:{chat_id}",
            ),
            button("🗑", f"remove:{chat_id}"),
        ]
        for chat_id, title, active in rows
    ]
    keyboard.append([button("⬅️ В меню", "home")])

    if rows:
        hint = "Нажми на чат — включить или выключить. 🗑 — отключить совсем."
    else:
        hint = "Чатов пока нет."
    text = (
        f"💬 <b>ЧАТЫ</b>\n{LINE}\n{hint}\n\n"
        "<i>Новый чат: добавь бота в группу и отправь там</i> <code>/addchat</code>"
    )
    return text, kb(keyboard)


def keywords_view():
    rows = db.execute("SELECT id, word FROM keywords ORDER BY id").fetchall()
    keyboard = [[button(f"🗑 {short(word)}", f"kwdel:{kid}")] for kid, word in rows]
    keyboard += [
        [button("➕ Добавить слово", "kwadd")],
        [button("⬅️ В меню", "home")],
    ]
    words = ", ".join(f"<code>{esc(w)}</code>" for _, w in rows) or "<i>нет</i>"
    text = (
        f"🔑 <b>КЛЮЧЕВЫЕ СЛОВА</b>\n{LINE}\n{words}\n\n"
        f"<i>Сообщения длиннее {MAX_WORDS} слов игнорируются, "
        "кроме «дай(те) номер».\nНажми на слово, чтобы удалить его.</i>"
    )
    return text, kb(keyboard)


def stats_view():
    total, free = number_counts()
    chats = db.execute("SELECT COUNT(*) FROM chats WHERE enabled=1").fetchone()[0]
    words = db.execute("SELECT COUNT(*) FROM keywords").fetchone()[0]

    today = time.localtime()
    start = int(time.mktime(today[:3] + (0, 0, 0, 0, 0, -1)))
    issued_today = db.execute(
        "SELECT COUNT(*) FROM numbers WHERE used=1 AND used_at>=?", (start,)
    ).fetchone()[0]
    issued_week = db.execute(
        "SELECT COUNT(*) FROM numbers WHERE used=1 AND used_at>=?",
        (int(time.time()) - 7 * 86400,),
    ).fetchone()[0]

    top = db.execute("""
        SELECT COALESCE(c.title, n.used_chat), COUNT(*) AS cnt
        FROM numbers n
        LEFT JOIN chats c ON c.chat_id = n.used_chat
        WHERE n.used=1 AND n.used_chat IS NOT NULL
        GROUP BY n.used_chat
        ORDER BY cnt DESC
        LIMIT 3
    """).fetchall()
    top_text = "\n".join(
        f"  {i}. {esc(short(t, 26))} — <b>{n}</b>" for i, (t, n) in enumerate(top, 1)
    ) or "  —"

    text = (
        f"📊 <b>СТАТИСТИКА</b>\n{LINE}\n"
        f"Состояние: {'🟢 включён' if enabled() else '🔴 выключен'}\n\n"
        f"📱 Всего номеров: <b>{total}</b>\n"
        f"✅ Свободно: <b>{free}</b>\n"
        f"📤 Выдано: <b>{total - free}</b>\n"
        f"<code>{bar(free, total)}</code>\n\n"
        f"📅 Выдано сегодня: <b>{issued_today}</b>\n"
        f"🗓 За 7 дней: <b>{issued_week}</b>\n\n"
        f"🏆 <b>Топ чатов</b>\n{top_text}\n\n"
        f"💬 Активных чатов: <b>{chats}</b>\n"
        f"🔑 Ключевых слов: <b>{words}</b>"
    )
    return text, kb([[button("🔄 Обновить", "stats"), button("⬅️ В меню", "home")]])


# --------------------------------------------------------------------- Команды

@dp.message(Command("start", "panel", "menu"))
async def start(m: Message):
    if m.chat.type != "private":
        return
    if m.from_user and is_admin(m.from_user.id):
        pending.pop(m.from_user.id, None)
        text, markup = panel_view()
        await m.answer(text, reply_markup=markup)
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
        await m.answer("Используй: <code>/add +79990000000</code>")
        return

    added, _ = add_numbers([parts[1]])
    await m.answer("✅ Номер добавлен!" if added else "Такой номер уже есть.")


@dp.message(Command("addmany"))
async def add_many(m: Message):
    if not is_admin_private(m):
        return

    parts = m.text.split(maxsplit=1)
    raw = parts[1] if len(parts) > 1 else ""
    if not raw.strip():
        await m.answer("Используй:\n<code>/addmany\n+79990000001\n+79990000002</code>")
        return

    added, skipped = add_numbers(raw.splitlines())
    await m.answer(f"✅ Добавлено: <b>{added}</b>\nПропущено (уже были): <b>{skipped}</b>")


@dp.message(Command("list"))
async def list_numbers(m: Message):
    if is_admin_private(m):
        text, markup = free_view(0)
        await m.answer(text, reply_markup=markup)


@dp.message(Command("history"))
async def history(m: Message):
    if is_admin_private(m):
        text, markup = history_view(0)
        await m.answer(text, reply_markup=markup)


@dp.message(Command("restore"))
async def restore_number(m: Message):
    """Вернуть выданный номер обратно в свободные."""
    if not is_admin_private(m):
        return

    parts = m.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().isdigit():
        await m.answer("Используй: <code>/restore ID</code>")
        return

    if restore_by_id(int(parts[1])):
        await m.answer("✅ Номер снова свободен.")
    else:
        await m.answer("Номер не найден или не выдан.")


@dp.message(Command("delete"))
async def delete_number(m: Message):
    if not is_admin_private(m):
        return

    parts = m.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().isdigit():
        await m.answer("Используй: <code>/delete ID</code>")
        return

    cur = db.execute("DELETE FROM numbers WHERE id=?", (int(parts[1]),))
    db.commit()
    await m.answer("✅ Удалено." if cur.rowcount else "Номер не найден.")


def restore_by_id(number_id):
    cur = db.execute("""
        UPDATE numbers
        SET used=0, used_at=NULL, used_by=NULL, used_by_name=NULL, used_chat=NULL
        WHERE id=? AND used=1
    """, (number_id,))
    db.commit()
    if cur.rowcount:
        numbers_added()
    return bool(cur.rowcount)


# -------------------------------------------------------------- Кнопки (меню)

@dp.callback_query()
async def callbacks(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True)
        return

    if c.message is None or not c.data:
        await c.answer()
        return

    data = c.data
    toast = None

    # Любая навигация отменяет ожидание ввода
    if data not in ("noop",):
        pending.pop(c.from_user.id, None)

    if data == "noop":
        pass

    elif data == "home":
        await safe_edit(c, *panel_view())

    elif data == "bot:toggle":
        set_setting("enabled", "0" if enabled() else "1")
        await safe_edit(c, *panel_view())
        toast = "Выдача включена" if enabled() else "Выдача выключена"

    elif data == "notify:toggle":
        set_setting("notify", "0" if state["notify"] else "1")
        await safe_edit(c, *panel_view())
        toast = "Уведомления включены" if state["notify"] else "Уведомления выключены"

    elif data == "numbers":
        await safe_edit(c, *numbers_view())

    elif data == "numadd":
        pending[c.from_user.id] = "numbers"
        await safe_edit(
            c,
            "➕ <b>ДОБАВИТЬ НОМЕРА</b>\n\n"
            "Отправь один номер или список — каждый с новой строки.\n"
            "Повторы бот пропустит сам.",
            kb([[button("✖️ Отмена", "numbers")]]),
        )

    elif data.startswith("free:"):
        await safe_edit(c, *free_view(int(data.split(":")[1])))

    elif data.startswith("hist:"):
        await safe_edit(c, *history_view(int(data.split(":")[1])))

    elif data.startswith("restore:"):
        _, nid, where = data.split(":")
        ok = restore_by_id(int(nid))
        toast = "↩️ Номер возвращён" if ok else "Номер уже свободен"
        if where == "n":  # нажали под уведомлением о выдаче
            if ok:
                await safe_edit(
                    c,
                    c.message.html_text + "\n\n↩️ <i>Номер возвращён в свободные</i>",
                    None,
                )
        else:             # нажали в истории
            await safe_edit(c, *history_view(int(where)))

    elif data == "numclear":
        await safe_edit(c, *clear_view())

    elif data == "numclear:yes":
        cur = db.execute("DELETE FROM numbers WHERE used=1")
        db.commit()
        toast = f"Удалено: {cur.rowcount}"
        await safe_edit(c, *numbers_view())

    elif data == "chats":
        await safe_edit(c, *chats_view())

    elif data.startswith("chat:"):
        cid = int(data.split(":")[1])
        db.execute("UPDATE chats SET enabled=1-enabled WHERE chat_id=?", (cid,))
        db.commit()
        reload_chats()
        await safe_edit(c, *chats_view())

    elif data.startswith("remove:"):
        cid = int(data.split(":")[1])
        db.execute("DELETE FROM chats WHERE chat_id=?", (cid,))
        db.commit()
        reload_chats()
        await safe_edit(c, *chats_view())
        toast = "Чат отключён"

    elif data == "keywords":
        await safe_edit(c, *keywords_view())

    elif data == "kwadd":
        pending[c.from_user.id] = "keyword"
        await safe_edit(
            c,
            "✏️ <b>НОВОЕ СЛОВО</b>\n\nОтправь ключевое слово или фразу.",
            kb([[button("✖️ Отмена", "keywords")]]),
        )

    elif data.startswith("kwdel:"):
        kid = int(data.split(":")[1])
        db.execute("DELETE FROM keywords WHERE id=?", (kid,))
        db.commit()
        reload_keywords()
        await safe_edit(c, *keywords_view())

    elif data == "stats":
        await safe_edit(c, *stats_view())

    await c.answer(toast)


# ------------------------------------------- Ввод текста (слова и номера)

def awaiting_input(m: Message):
    return (
        m.chat.type == "private"
        and m.from_user is not None
        and is_admin(m.from_user.id)
        and pending.get(m.from_user.id) in ("keyword", "numbers")
        and not (m.text or "").startswith("/")  # команды не считаем вводом
    )


@dp.message(F.text, awaiting_input)
async def pending_input(m: Message):
    kind = pending.pop(m.from_user.id, None)

    if kind == "numbers":
        added, skipped = add_numbers(m.text.splitlines())
        await m.answer(
            f"✅ Добавлено: <b>{added}</b>\nПропущено (уже были): <b>{skipped}</b>",
            reply_markup=kb([[button("⬅️ К номерам", "numbers")]]),
        )
        return

    word = m.text.strip()
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
    await m.answer(
        f"✅ Добавлено: <code>{esc(word)}</code>",
        reply_markup=kb([[button("⬅️ К словам", "keywords")]]),
    )


# ---------------------------------------------- Уведомление админу о выдаче

async def notify_admin(m: Message, number_id, number, text):
    """Присылает админу в бота: кому выдан номер + ссылка на сообщение в чате."""
    try:
        user = m.from_user
        mention = f'<a href="tg://user?id={user.id}">{esc(user.full_name)}</a>'
        username = f" (@{esc(user.username)})" if user.username else ""
        _, free = number_counts()

        lines = [
            "🔔 <b>Выдан номер</b>",
            LINE,
            f"📱 <code>{esc(number)}</code>",
            f"👤 {mention}{username}",
            f"💬 {esc(m.chat.title or m.chat.id)}",
            f"✉️ <i>{esc(short(text, 80))}</i>",
            "",
            f"Осталось свободных: <b>{free}</b>",
        ]
        if free <= LOW_STOCK:
            lines.append("⚠️ <b>Номера заканчиваются!</b> Пополни: /addmany")

        row = []
        link = message_link(m)
        if link:
            row.append(InlineKeyboardButton(text="🔗 Открыть сообщение", url=link))
        row.append(button("↩️ Вернуть номер", f"restore:{number_id}:n"))

        await bot.send_message(ADMIN_ID, "\n".join(lines), reply_markup=kb([row]))
    except Exception:
        log.exception("Не удалось отправить уведомление админу")


def spawn(coro):
    task = asyncio.create_task(coro)
    background.add(task)
    task.add_done_callback(background.discard)


# ---------------------------------------------------------- Автоматическая выдача

async def handle_empty(m: Message):
    """Номера закончились: не спамим чат и один раз предупреждаем админа."""
    now = time.monotonic()
    if now - empty_notice.get(m.chat.id, float("-inf")) > EMPTY_NOTICE_COOLDOWN:
        empty_notice[m.chat.id] = now
        try:
            await m.reply("😔 Свободные номера закончились.")
        except Exception:
            log.exception("Не удалось отправить сообщение о конце номеров")

    if not state["admin_notified"]:
        state["admin_notified"] = True
        try:
            await bot.send_message(
                ADMIN_ID,
                "⚠️ <b>Свободные номера закончились.</b>\nДобавь новые: /addmany",
            )
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

    # Между SELECT и UPDATE нет await, поэтому два сообщения не получат один номер.
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
        await m.reply(f"📱 Номер: <code>{esc(row[1])}</code>")
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

    # Уведомление уходит в фоне и не тормозит выдачу
    if state["notify"]:
        spawn(notify_admin(m, row[0], row[1], m.text))

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
    reload_settings()
    reload_chats()
    log.info("NUMBER BOT %s запущен!", VERSION)
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
