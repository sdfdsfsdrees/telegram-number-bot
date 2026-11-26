import asyncio
import html
import logging
import math
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

# ============================================================ НАСТРОЙКИ
BOT_TOKEN = os.getenv("BOT_TOKEN", "ВСТАВЬ_НОВЫЙ_ТОКЕН")  # токен от @BotFather (лучше через переменную окружения)
ADMIN_ID = int(os.getenv("ADMIN_ID", "1650081425"))         # твой Telegram ID

# Прокси, если Telegram заблокирован. Примеры:
#   "http://127.0.0.1:10809"   (HTTP-прокси от v2rayN и т.п.)
#   "socks5://127.0.0.1:10808" (нужно: pip install aiohttp_socks)
# Если используешь VPN на весь компьютер — оставь пустым.
PROXY = os.getenv("PROXY", "")
REQUEST_TIMEOUT = 60      # таймаут запросов к Telegram (сек)

USER_COOLDOWN = 30        # пауза между выдачами одному человеку в чате (сек), 0 = без паузы
MAX_WORDS = 4             # сообщения длиннее этого числа слов игнорируются...
ALLOW_PHRASES = ("дайте номер", "дай номер", "дайте номерок", "дай номерок")  # ...кроме этих фраз
LOW_STOCK = 5             # предупреждать, когда свободных номеров столько или меньше
EMPTY_NOTICE_COOLDOWN = 9999  # как часто писать в чат «номера закончились» (сек)
# ================================================================

if not BOT_TOKEN or BOT_TOKEN.startswith("ВСТАВЬ") or not ADMIN_ID:
    sys.exit("Впиши BOT_TOKEN и ADMIN_ID в начале файла bot.py и запусти снова.")

try:  # быстрый цикл событий (Linux/macOS)
    import uvloop
    uvloop.install()
except ImportError:
    pass

VERSION = "4.3"
DB_PATH = Path(__file__).resolve().parent / "numbers.db"
LINE = "━━━━━━━━━━━━━━━━━━"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("number_bot")

session = AiohttpSession(proxy=PROXY or None, timeout=REQUEST_TIMEOUT)
bot = Bot(BOT_TOKEN, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

db = sqlite3.connect(DB_PATH, check_same_thread=False)
for pragma in ("journal_mode=WAL", "busy_timeout=5000", "synchronous=NORMAL"):
    db.execute(f"PRAGMA {pragma}")

pending = {}          # user_id -> ожидаемый ввод ("keyword" / "numbers")
last_issue = {}       # (chat_id, user_id) -> время последней выдачи
empty_notice = {}     # chat_id -> время последнего сообщения «закончились»
background = set()    # ссылки на фоновые задачи
active_chats = set()  # chat_id включённых чатов
state = {"admin_notified": False, "kw_regex": None, "enabled": True, "notify": True}


# ---------------------------------------------------------------- База данных

def add_column(table, column, ddl):
    if column not in [r[1] for r in db.execute(f"PRAGMA table_info({table})")]:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def init_db():
    db.executescript("""
        CREATE TABLE IF NOT EXISTS numbers (
            id INTEGER PRIMARY KEY AUTOINCREMENT, number TEXT NOT NULL, used INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS chats (
            chat_id INTEGER PRIMARY KEY, title TEXT DEFAULT '', enabled INTEGER DEFAULT 1);
        CREATE TABLE IF NOT EXISTS keywords (
            id INTEGER PRIMARY KEY AUTOINCREMENT, word TEXT UNIQUE COLLATE NOCASE);
        CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY, value TEXT);
    """)
    # Миграции старой базы
    add_column("chats", "title", "TEXT DEFAULT ''")
    add_column("numbers", "used", "INTEGER DEFAULT 0")
    add_column("numbers", "used_at", "INTEGER")
    add_column("numbers", "used_by", "INTEGER")
    add_column("numbers", "used_by_name", "TEXT")
    add_column("numbers", "used_chat", "INTEGER")

    db.execute("INSERT OR IGNORE INTO settings VALUES ('enabled', '1')")
    db.execute("INSERT OR IGNORE INTO settings VALUES ('notify', '1')")
    db.executemany("INSERT OR IGNORE INTO keywords (word) VALUES (?)", [("слет",), ("номер",)])
    db.execute("CREATE INDEX IF NOT EXISTS idx_numbers_used ON numbers(used, id)")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_numbers_number ON numbers(number)")
    db.commit()


init_db()
FREE_SQL = "used=0, used_at=NULL, used_by=NULL, used_by_name=NULL, used_chat=NULL"


# ------------------------------------------------------------------- Помощники

def is_admin(user_id):
    return user_id == ADMIN_ID


def esc(value):
    return html.escape(str(value), quote=False)


def short(text, limit=30):
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def norm(text):
    """Нижний регистр + ё -> е."""
    return text.casefold().replace("ё", "е")


def bar(free, total, width=10):
    filled = round(width * free / total) if total else 0
    return "▰" * filled + "▱" * (width - filled)


def stamp(ts):
    return time.strftime("%d.%m %H:%M", time.localtime(ts)) if ts else "—"


def count(sql, *args):
    return db.execute(sql, args).fetchone()[0]


def number_counts():
    return db.execute("SELECT COUNT(*), COALESCE(SUM(used=0), 0) FROM numbers").fetchone()


ALLOW_REGEX = re.compile(
    r"(?<!\w)(?:"
    + "|".join(re.escape(norm(p)).replace("\\ ", r"\s+") for p in ALLOW_PHRASES)
    + r")(?!\w)"
)


def reload_keywords():
    words = [r[0] for r in db.execute("SELECT word FROM keywords")]
    if not words:
        state["kw_regex"] = None
        return
    parts = sorted({re.escape(norm(w)) for w in words}, key=len, reverse=True)
    state["kw_regex"] = re.compile(r"(?<!\w)(?:" + "|".join(parts) + r")(?!\w)")


def reload_settings():
    rows = dict(db.execute("SELECT name, value FROM settings"))
    state["enabled"] = rows.get("enabled", "1") == "1"
    state["notify"] = rows.get("notify", "1") == "1"


def reload_chats():
    active_chats.clear()
    active_chats.update(r[0] for r in db.execute("SELECT chat_id FROM chats WHERE enabled=1"))


def set_setting(name, value):
    db.execute(
        "INSERT INTO settings (name, value) VALUES (?, ?) "
        "ON CONFLICT(name) DO UPDATE SET value=excluded.value", (name, value))
    db.commit()
    reload_settings()


def add_numbers(numbers):
    """Возвращает (добавлено, пропущено). Дубликаты отсекает UNIQUE-индекс."""
    numbers = list(dict.fromkeys(n.strip() for n in numbers if n.strip() and len(n.strip()) <= 32))
    before = db.total_changes
    db.executemany("INSERT OR IGNORE INTO numbers (number) VALUES (?)", [(n,) for n in numbers])
    db.commit()
    added = db.total_changes - before
    if added:
        state["admin_notified"] = False
    return added, len(numbers) - added


def restore_by_id(number_id):
    cur = db.execute(f"UPDATE numbers SET {FREE_SQL} WHERE id=? AND used=1", (number_id,))
    db.commit()
    if cur.rowcount:
        state["admin_notified"] = False
    return bool(cur.rowcount)


def message_link(m: Message):
    if m.chat.username:
        base = f"https://t.me/{m.chat.username}"
    elif str(m.chat.id).startswith("-100"):
        base = f"https://t.me/c/{str(m.chat.id)[4:]}"
    else:
        return None
    if getattr(m, "is_topic_message", False) and m.message_thread_id:
        return f"{base}/{m.message_thread_id}/{m.message_id}"
    return f"{base}/{m.message_id}"


async def safe_edit(c: CallbackQuery, text, markup):
    try:
        await c.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


def spawn(coro):
    task = asyncio.create_task(coro)
    background.add(task)
    task.add_done_callback(background.discard)


# ------------------------------------------------------------------- Экраны

def button(text, data):
    return InlineKeyboardButton(text=text, callback_data=data)


def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


def pager(prefix, page, pages):
    row = [button("◀️", f"{prefix}:{page - 1}")] if page > 0 else []
    row.append(button(f"{page + 1}/{pages}", "noop"))
    if page < pages - 1:
        row.append(button("▶️", f"{prefix}:{page + 1}"))
    return row


def clamp_page(page, total, per_page):
    pages = max(1, math.ceil(total / per_page))
    return max(0, min(page, pages - 1)), pages


def panel_view():
    total, free = number_counts()
    chats = count("SELECT COUNT(*) FROM chats WHERE enabled=1")
    words = count("SELECT COUNT(*) FROM keywords")
    status = "🟢 <b>Выдача включена</b>" if state["enabled"] else "🔴 <b>Выдача выключена</b>"
    text = (
        f"🤖 <b>NUMBER BOT</b>  <i>v{VERSION}</i>\n{LINE}\n{status}\n\n"
        f"📱 Номера: <b>{free}</b> свободно из <b>{total}</b>\n"
        f"<code>{bar(free, total)}</code>\n\n"
        f"💬 Активных чатов: <b>{chats}</b>\n"
        f"🔑 Ключевых слов: <b>{words}</b>\n"
        f"🔔 Уведомления: <b>{'включены' if state['notify'] else 'выключены'}</b>"
    )
    return text, kb([
        [button("🔴 Выключить выдачу" if state["enabled"] else "🟢 Включить выдачу", "bot:toggle")],
        [button("📱 Номера", "numbers"), button("💬 Чаты", "chats")],
        [button("🔑 Слова", "keywords"), button("📊 Статистика", "stats")],
        [button("🔔 Уведомления: вкл" if state["notify"] else "🔕 Уведомления: выкл", "notify:toggle")],
    ])


def numbers_view():
    total, free = number_counts()
    text = (
        f"📱 <b>НОМЕРА</b>\n{LINE}\n"
        f"Свободно: <b>{free}</b>\nВыдано: <b>{total - free}</b>\nВсего: <b>{total}</b>\n"
        f"<code>{bar(free, total)}</code>\n\n"
        "<i>Командами:</i>\n"
        "<code>/add +79990000000</code> — один номер\n"
        "<code>/delete ID</code> — удалить по ID"
    )
    return text, kb([
        [button("➕ Добавить", "numadd"), button("📋 Свободные", "free:0")],
        [button("📜 История", "hist:0"), button("🧹 Очистить выданные", "numclear")],
        [button("⬅️ В меню", "home")],
    ])


def free_view(page=0):
    _, free = number_counts()
    per_page = 10
    page, pages = clamp_page(page, free, per_page)
    rows = db.execute(
        "SELECT id, number FROM numbers WHERE used=0 ORDER BY id LIMIT ? OFFSET ?",
        (per_page, page * per_page)).fetchall()
    body = "\n".join(f"<code>{i}</code> · {esc(n)}" for i, n in rows) or "<i>Свободных номеров нет.</i>"
    return (f"📋 <b>СВОБОДНЫЕ НОМЕРА</b>  ({free})\n{LINE}\n{body}",
            kb([pager("free", page, pages), [button("⬅️ К номерам", "numbers")]]))


def history_view(page=0):
    total = count("SELECT COUNT(*) FROM numbers WHERE used=1")
    per_page = 8
    page, pages = clamp_page(page, total, per_page)
    rows = db.execute("""
        SELECT n.id, n.number, n.used_at, n.used_by_name, COALESCE(c.title, n.used_chat)
        FROM numbers n LEFT JOIN chats c ON c.chat_id = n.used_chat
        WHERE n.used=1 ORDER BY n.used_at DESC, n.id DESC LIMIT ? OFFSET ?
    """, (per_page, page * per_page)).fetchall()

    body = "\n\n".join(
        f"<code>{nid}</code> · <b>{esc(number)}</b>\n"
        f"    👤 {esc(who or '—')}  💬 {esc(short(chat or '—', 24))}\n    🕒 {stamp(ts)}"
        for nid, number, ts, who, chat in rows
    ) or "<i>Выдач пока не было.</i>"
    if rows:
        body += "\n\n<i>↩️ — вернуть номер в свободные</i>"

    restore = [button(f"↩️ {r[0]}", f"restore:{r[0]}:{page}") for r in rows]
    keyboard = [restore[i:i + 4] for i in range(0, len(restore), 4)]
    keyboard += [pager("hist", page, pages), [button("⬅️ К номерам", "numbers")]]
    return f"📜 <b>ИСТОРИЯ ВЫДАЧ</b>  ({total})\n{LINE}\n{body}", kb(keyboard)


def clear_view():
    used = count("SELECT COUNT(*) FROM numbers WHERE used=1")
    return (
        f"🧹 <b>Очистить выданные?</b>\n\nБудет удалено номеров: <b>{used}</b>.\n"
        "История выдач пропадёт. Свободные номера не тронем.",
        kb([[button("✅ Да, удалить", "numclear:yes"), button("✖️ Отмена", "numbers")]]),
    )


def chats_view():
    rows = db.execute("SELECT chat_id, title, enabled FROM chats ORDER BY title").fetchall()
    keyboard = [
        [button(f"{'🟢' if active else '🔴'} {short(title or chat_id)}", f"chat:{chat_id}"),
         button("🗑", f"remove:{chat_id}")]
        for chat_id, title, active in rows
    ] + [[button("⬅️ В меню", "home")]]
    hint = "Нажми на чат — включить или выключить. 🗑 — отключить совсем." if rows else "Чатов пока нет."
    return (f"💬 <b>ЧАТЫ</b>\n{LINE}\n{hint}\n\n"
            "<i>Новый чат: добавь бота в группу и отправь там</i> <code>/addchat</code>",
            kb(keyboard))


def keywords_view():
    rows = db.execute("SELECT id, word FROM keywords ORDER BY id").fetchall()
    keyboard = [[button(f"🗑 {short(word)}", f"kwdel:{kid}")] for kid, word in rows]
    keyboard += [[button("➕ Добавить слово", "kwadd")], [button("⬅️ В меню", "home")]]
    words = ", ".join(f"<code>{esc(w)}</code>" for _, w in rows) or "<i>нет</i>"
    return (f"🔑 <b>КЛЮЧЕВЫЕ СЛОВА</b>\n{LINE}\n{words}\n\n"
            f"<i>Сообщения длиннее {MAX_WORDS} слов игнорируются, кроме «дай(те) номер».\n"
            "Нажми на слово, чтобы удалить его.</i>", kb(keyboard))


def stats_view():
    total, free = number_counts()
    day_start = int(time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1)))
    today = count("SELECT COUNT(*) FROM numbers WHERE used=1 AND used_at>=?", day_start)
    week = count("SELECT COUNT(*) FROM numbers WHERE used=1 AND used_at>=?", int(time.time()) - 7 * 86400)
    top = db.execute("""
        SELECT COALESCE(c.title, n.used_chat), COUNT(*) AS cnt
        FROM numbers n LEFT JOIN chats c ON c.chat_id = n.used_chat
        WHERE n.used=1 AND n.used_chat IS NOT NULL
        GROUP BY n.used_chat ORDER BY cnt DESC LIMIT 3
    """).fetchall()
    top_text = "\n".join(
        f"  {i}. {esc(short(t, 26))} — <b>{n}</b>" for i, (t, n) in enumerate(top, 1)) or "  —"
    text = (
        f"📊 <b>СТАТИСТИКА</b>\n{LINE}\n"
        f"Состояние: {'🟢 включён' if state['enabled'] else '🔴 выключен'}\n\n"
        f"📱 Всего номеров: <b>{total}</b>\n✅ Свободно: <b>{free}</b>\n"
        f"📤 Выдано: <b>{total - free}</b>\n<code>{bar(free, total)}</code>\n\n"
        f"📅 Выдано сегодня: <b>{today}</b>\n🗓 За 7 дней: <b>{week}</b>\n\n"
        f"🏆 <b>Топ чатов</b>\n{top_text}"
    )
    return text, kb([[button("🔄 Обновить", "stats"), button("⬅️ В меню", "home")]])


# Простые экраны без параметров
SCREENS = {
    "home": panel_view, "numbers": numbers_view, "chats": chats_view,
    "keywords": keywords_view, "stats": stats_view, "numclear": clear_view,
}
# Экраны со страницей: "free:3", "hist:0"
PAGED = {"free": free_view, "hist": history_view}


# --------------------------------------------------------------------- Команды

def admin_private(m: Message):
    return bool(m.from_user) and is_admin(m.from_user.id) and m.chat.type == "private"


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
        INSERT INTO chats (chat_id, title, enabled) VALUES (?, ?, 1)
        ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title, enabled=1
    """, (m.chat.id, m.chat.title or "Группа"))
    db.commit()
    reload_chats()
    await m.answer("✅ Чат подключён!")


@dp.message(Command("add"))
async def add_number(m: Message):
    if not admin_private(m):
        return
    parts = m.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await m.answer("Используй: <code>/add +79990000000</code>\n"
                       "Список — каждый номер с новой строки.")
        return
    added, skipped = add_numbers(parts[1].splitlines())
    await m.answer(f"✅ Добавлено: <b>{added}</b>\nПропущено (уже были): <b>{skipped}</b>")


@dp.message(Command("delete"))
async def delete_number(m: Message):
    if not admin_private(m):
        return
    parts = m.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().isdigit():
        await m.answer("Используй: <code>/delete ID</code>")
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
    if c.message is None or not c.data:
        await c.answer()
        return

    data, toast = c.data, None
    action, _, arg = data.partition(":")
    if data != "noop":
        pending.pop(c.from_user.id, None)  # любая навигация отменяет ожидание ввода

    if data in SCREENS:
        await safe_edit(c, *SCREENS[data]())

    elif action in PAGED:
        await safe_edit(c, *PAGED[action](int(arg)))

    elif data == "bot:toggle":
        set_setting("enabled", "0" if state["enabled"] else "1")
        await safe_edit(c, *panel_view())
        toast = "Выдача включена" if state["enabled"] else "Выдача выключена"

    elif data == "notify:toggle":
        set_setting("notify", "0" if state["notify"] else "1")
        await safe_edit(c, *panel_view())
        toast = "Уведомления включены" if state["notify"] else "Уведомления выключены"

    elif data == "numadd":
        pending[c.from_user.id] = "numbers"
        await safe_edit(
            c, "➕ <b>ДОБАВИТЬ НОМЕРА</b>\n\nОтправь один номер или список — "
               "каждый с новой строки.\nПовторы бот пропустит сам.",
            kb([[button("✖️ Отмена", "numbers")]]))

    elif action == "restore":
        nid, where = arg.split(":")
        ok = restore_by_id(int(nid))
        toast = "↩️ Номер возвращён" if ok else "Номер уже свободен"
        if where == "n":  # нажали под уведомлением о выдаче
            if ok:
                await safe_edit(c, c.message.html_text + "\n\n↩️ <i>Номер возвращён в свободные</i>", None)
        else:             # нажали в истории
            await safe_edit(c, *history_view(int(where)))

    elif data == "numclear:yes":
        cur = db.execute("DELETE FROM numbers WHERE used=1")
        db.commit()
        toast = f"Удалено: {cur.rowcount}"
        await safe_edit(c, *numbers_view())

    elif action == "chat":
        db.execute("UPDATE chats SET enabled=1-enabled WHERE chat_id=?", (int(arg),))
        db.commit()
        reload_chats()
        await safe_edit(c, *chats_view())

    elif action == "remove":
        db.execute("DELETE FROM chats WHERE chat_id=?", (int(arg),))
        db.commit()
        reload_chats()
        await safe_edit(c, *chats_view())
        toast = "Чат отключён"

    elif data == "kwadd":
        pending[c.from_user.id] = "keyword"
        await safe_edit(c, "✏️ <b>НОВОЕ СЛОВО</b>\n\nОтправь ключевое слово или фразу.",
                        kb([[button("✖️ Отмена", "keywords")]]))

    elif action == "kwdel":
        db.execute("DELETE FROM keywords WHERE id=?", (int(arg),))
        db.commit()
        reload_keywords()
        await safe_edit(c, *keywords_view())

    await c.answer(toast)


# ------------------------------------------- Ввод текста (слова и номера)

def awaiting_input(m: Message):
    return (
        admin_private(m)
        and pending.get(m.from_user.id) in ("keyword", "numbers")
        and not (m.text or "").startswith("/")
    )


@dp.message(F.text, awaiting_input)
async def pending_input(m: Message):
    kind = pending.pop(m.from_user.id, None)

    if kind == "numbers":
        added, skipped = add_numbers(m.text.splitlines())
        await m.answer(f"✅ Добавлено: <b>{added}</b>\nПропущено (уже были): <b>{skipped}</b>",
                       reply_markup=kb([[button("⬅️ К номерам", "numbers")]]))
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
    await m.answer(f"✅ Добавлено: <code>{esc(word)}</code>",
                   reply_markup=kb([[button("⬅️ К словам", "keywords")]]))


# ---------------------------------------------- Уведомления админу

async def notify_admin(m: Message, number_id, number):
    try:
        user = m.from_user
        username = f" (@{esc(user.username)})" if user.username else ""
        _, free = number_counts()
        lines = [
            "🔔 <b>Выдан номер</b>", LINE,
            f"📱 <code>{esc(number)}</code>",
            f'👤 <a href="tg://user?id={user.id}">{esc(user.full_name)}</a>{username}',
            f"💬 {esc(m.chat.title or m.chat.id)}",
            f"✉️ <i>{esc(short(m.text, 80))}</i>",
            "", f"Осталось свободных: <b>{free}</b>",
        ]
        if free <= LOW_STOCK:
            lines.append("⚠️ <b>Номера заканчиваются!</b> Пополни: /add")

        row = []
        link = message_link(m)
        if link:
            row.append(InlineKeyboardButton(text="🔗 Открыть сообщение", url=link))
        row.append(button("↩️ Вернуть номер", f"restore:{number_id}:n"))
        await bot.send_message(ADMIN_ID, "\n".join(lines), reply_markup=kb([row]))
    except Exception:
        log.exception("Не удалось отправить уведомление админу")


async def handle_empty(m: Message):
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
                ADMIN_ID, "⚠️ <b>Свободные номера закончились.</b>\nДобавь новые: /add")
        except Exception:
            log.exception("Не удалось уведомить админа")


# ---------------------------------------------------------- Автоматическая выдача

@dp.message(F.text)
async def auto_reply(m: Message):
    # Самые дешёвые проверки — первыми, чтобы не тратить время на чужие сообщения
    if m.chat.type == "private" or not m.from_user or m.from_user.is_bot:
        return
    if not state["enabled"] or m.chat.id not in active_chats:
        return

    regex = state["kw_regex"]
    text = norm(m.text)
    if regex is None or not regex.search(text):
        return
    if len(text.split()) > MAX_WORDS and not ALLOW_REGEX.search(text):
        return

    user_id = m.from_user.id
    now = time.monotonic()
    if USER_COOLDOWN and not is_admin(user_id):
        last = last_issue.get((m.chat.id, user_id))
        if last is not None and now - last < USER_COOLDOWN:
            return

    who = f"@{m.from_user.username}" if m.from_user.username else m.from_user.full_name
    # Атомарно: выбрать первый свободный номер и сразу пометить выданным
    row = db.execute(
        """UPDATE numbers SET used=1, used_at=?, used_by=?, used_by_name=?, used_chat=?
           WHERE id = (SELECT id FROM numbers WHERE used=0 ORDER BY id LIMIT 1)
           RETURNING id, number""",
        (int(time.time()), user_id, who, m.chat.id)).fetchone()
    db.commit()

    if not row:
        await handle_empty(m)
        return
    number_id, number = row

    try:
        await m.reply(f"📱 Номер: <code>{esc(number)}</code>")
    except Exception:
        log.exception("Ошибка отправки номера, возвращаю его в свободные")
        restore_by_id(number_id)
        return

    last_issue[(m.chat.id, user_id)] = now
    if len(last_issue) > 5000:
        for key in [k for k, t in last_issue.items() if now - t > USER_COOLDOWN]:
            del last_issue[key]

    if state["notify"]:
        spawn(notify_admin(m, number_id, number))

    log.info("Номер %s выдан %s в чате %s", number, who, m.chat.id)


# ----------------------------------------------------------------------- Запуск

async def main():
    reload_keywords()
    reload_settings()
    reload_chats()
    log.info("NUMBER BOT %s запущен! Прокси: %s", VERSION, PROXY or "нет")

    delay = 5
    try:
        while True:
            try:
                await dp.start_polling(
                    bot, allowed_updates=["message", "callback_query"], close_bot_session=False)
                break  # штатная остановка
            except TelegramNetworkError as e:
                log.error("Нет связи с Telegram (%s). Повтор через %s сек. "
                          "Проверь VPN/прокси.", e, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)
    finally:
        await bot.session.close()
        db.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
