import asyncio
import html
import logging
import math
import re
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery, CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup,
    Message, ReactionTypeEmoji,
)

# ============================================================ НАСТРОЙКИ
# Впиши один раз и сохрани файл. Дальше бот запускается сразу.
BOT_TOKEN = "9136637036:AAHY7_H1S61HzBtPGMfufoCRUCNdEneke9A"            # токен от @BotFather в кавычках, например "123456:ABC-DEF..."
ADMIN_ID = 1650081425     # твой Telegram ID (число)

USER_COOLDOWN = 30        # пауза между выдачами одному человеку в чате (сек), 0 = без паузы
PAUSE_REACTION = "👀"     # реакция на сообщение человека, который в паузе (⏳ Telegram не разрешает)
MAX_WORDS = 4             # сообщения длиннее этого числа слов игнорируются...
ALLOW_PHRASES = ("дайте номер", "дай номер", "дайте номерок", "дай номерок")  # ...кроме этих фраз
LOW_STOCK = 5             # 🔴 и предупреждение, когда свободных номеров столько или меньше
EMPTY_NOTICE_COOLDOWN = 9999  # как часто писать в чат «номера закончились» (сек)
# ================================================================

if not BOT_TOKEN or not ADMIN_ID:
    sys.exit("Впиши BOT_TOKEN и ADMIN_ID в начале файла bot.py и запусти снова.")

try:  # быстрый цикл событий (Linux/macOS); на Windows пропускается
    import uvloop
    uvloop.install()
except ImportError:
    pass

VERSION = "5.0"
DB_PATH = Path(__file__).resolve().parent / "numbers.db"
LINE = "━━━━━━━━━━━━━━━━━━"
TAP_GUARD = 1.0           # повторное нажатие той же кнопки быстрее этого (сек) игнорируется
ERROR_COOLDOWN = 3600     # как часто повторять админу ошибку по одному и тому же чату (сек)
CLEANUP_EVERY = 600       # как часто чистить словари в памяти (сек)
MAX_TOKENS = 40           # сколько слов сообщения анализировать
SPARK = "▁▂▃▄▅▆▇█"
WEEKDAYS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("number_bot")

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

db = sqlite3.connect(DB_PATH, check_same_thread=False)
for pragma in ("journal_mode=WAL", "busy_timeout=5000", "synchronous=NORMAL"):
    db.execute(f"PRAGMA {pragma}")

pending = {}          # user_id -> ожидаемый ввод ("keyword" / "numbers")
panel_msg = {}        # user_id -> id сообщения с меню (оно всегда одно)
last_issue = {}       # (chat_id, user_id) -> время последней выдачи
empty_notice = {}     # chat_id -> время последнего сообщения «закончились»
chat_errors = {}      # chat_id -> время последней жалобы админу
tap_times = {}        # (user_id, message_id, data) -> время последнего нажатия
background = set()    # ссылки на фоновые задачи
active_chats = set()  # chat_id включённых чатов
state = {"admin_notified": False, "keywords": [], "enabled": True, "notify": True}


# ---------------------------------------------------------------- База данных

def add_column(table, column, ddl):
    if column not in [r[1] for r in db.execute(f"PRAGMA table_info({table})")]:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


db.execute("""CREATE TABLE IF NOT EXISTS numbers (
    id INTEGER PRIMARY KEY AUTOINCREMENT, number TEXT NOT NULL, used INTEGER DEFAULT 0)""")
db.execute("""CREATE TABLE IF NOT EXISTS chats (
    chat_id INTEGER PRIMARY KEY, title TEXT DEFAULT '', enabled INTEGER DEFAULT 1)""")
db.execute("""CREATE TABLE IF NOT EXISTS keywords (
    id INTEGER PRIMARY KEY AUTOINCREMENT, word TEXT UNIQUE COLLATE NOCASE)""")
db.execute("CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY, value TEXT)")

# Миграции старой базы
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

FREE_SQL = ("used=0, used_at=NULL, used_by=NULL, used_by_name=NULL, used_chat=NULL")


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


def head(icon, *path):
    """Единый заголовок экрана с хлебными крошками: «📜 Номера › История»."""
    return f"{icon} <b>{' › '.join(path)}</b>\n{LINE}"


def stock_icon(free, total):
    if free <= LOW_STOCK:
        return "🔴"
    return "🟡" if free * 3 < total else "🟢"


def bar(free, total, width=10):
    fill = {"🟢": "🟩", "🟡": "🟨", "🔴": "🟥"}[stock_icon(free, total)]
    filled = round(width * free / total) if total else 0
    if free > 0:
        filled = max(filled, 1)
    return fill * filled + "⬜" * (width - filled)


def when(ts):
    if not ts:
        return "—"
    t = time.localtime(ts)
    same_day = time.strftime("%Y%m%d", t) == time.strftime("%Y%m%d")
    return time.strftime("%H:%M" if same_day else "%d.%m %H:%M", t)


def count(sql, *args):
    return db.execute(sql, args).fetchone()[0]


def number_counts():
    return db.execute("SELECT COUNT(*), COALESCE(SUM(used=0), 0) FROM numbers").fetchone()


def spawn(coro):
    task = asyncio.create_task(coro)
    background.add(task)
    task.add_done_callback(background.discard)


# ------------------------------------------- Распознавание слов (с опечатками)

def squeeze(word):
    """Схлопывает повторы букв: «слетт» -> «слет»."""
    return re.sub(r"(.)\1+", r"\1", word)


def prep(phrase):
    return tuple(squeeze(w) for w in re.findall(r"\w+", norm(phrase)))


def distance(a, b, limit):
    """Расстояние Дамерау-Левенштейна (опечатка и перестановка букв = 1)."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev2, prev = None, list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
            if i > 1 and j > 1 and ca == b[j - 2] and a[i - 2] == cb:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        prev2, prev = prev, cur
    return prev[-1]


@lru_cache(maxsize=4096)
def word_match(token, key):
    """Слово из сообщения подходит к ключевому: точно, с окончанием/суффиксом или с опечаткой."""
    if token == key:
        return True
    if len(key) < 4:                      # короткие слова только точно, иначе много ложных срабатываний
        return False
    extra = len(token) - len(key)
    if token.startswith(key) and extra <= 3:      # номер -> номера, номерок
        return True
    if len(key) < 5:
        return False
    limit = 1 if len(key) < 8 else 2
    if distance(token, key, limit) <= limit:
        return True
    return 0 < extra <= 3 and distance(token[:len(key)], key, limit) <= limit


def phrase_hit(stems, phrases):
    for phrase in phrases:
        n = len(phrase)
        for i in range(len(stems) - n + 1):
            if all(word_match(stems[i + j], phrase[j]) for j in range(n)):
                return True
    return False


ALLOW_TOKENS = [p for p in (prep(x) for x in ALLOW_PHRASES) if p]


def reload_keywords():
    state["keywords"] = [p for p in (prep(w) for (w,) in db.execute("SELECT word FROM keywords")) if p]


def reload_settings():
    rows = dict(db.execute("SELECT name, value FROM settings"))
    state["enabled"] = rows.get("enabled", "1") == "1"
    state["notify"] = rows.get("notify", "1") == "1"


def set_setting(name, value):
    db.execute(
        "INSERT INTO settings (name, value) VALUES (?, ?) "
        "ON CONFLICT(name) DO UPDATE SET value=excluded.value", (name, value))
    db.commit()
    reload_settings()


def reload_chats():
    active_chats.clear()
    active_chats.update(r[0] for r in db.execute("SELECT chat_id FROM chats WHERE enabled=1"))


def add_numbers(numbers):
    """Возвращает (добавлено, пропущено)."""
    numbers = list(dict.fromkeys(n.strip() for n in numbers if n.strip()))
    added = 0
    for number in numbers:
        if len(number) <= 32 and not db.execute(
                "SELECT 1 FROM numbers WHERE number=?", (number,)).fetchone():
            db.execute("INSERT INTO numbers (number) VALUES (?)", (number,))
            added += 1
    db.commit()
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


async def drop(chat_id, message_id):
    """Удаляет сообщение, ошибки не важны."""
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception:
        pass


async def show_panel(uid, view, note=None, fresh=False):
    """Меню всегда живёт в одном сообщении: правим его, а не шлём новые."""
    text, markup = view
    if note:
        text = f"{note}\n\n{text}"
    mid = panel_msg.get(uid)
    if mid and fresh:
        await drop(uid, panel_msg.pop(uid))
        mid = None
    if mid:
        try:
            await bot.edit_message_text(text, chat_id=uid, message_id=mid, reply_markup=markup)
            return
        except TelegramBadRequest as e:
            if "message is not modified" in str(e):
                return
    sent = await bot.send_message(uid, text, reply_markup=markup)
    panel_msg[uid] = sent.message_id


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


def panel_view():
    total, free = number_counts()
    chats = count("SELECT COUNT(*) FROM chats WHERE enabled=1")
    words = count("SELECT COUNT(*) FROM keywords")
    status = "🟢 <b>Выдача включена</b>" if state["enabled"] else "🔴 <b>Выдача выключена</b>"
    text = (
        f"{head('🤖', 'NUMBER BOT')}\n{status}\n\n"
        f"📱 Номера: {stock_icon(free, total)} <b>{free}</b> свободно из <b>{total}</b>\n"
        f"{bar(free, total)}\n\n"
        f"💬 Активных чатов: <b>{chats}</b>\n"
        f"🔑 Ключевых слов: <b>{words}</b>\n"
        f"🔔 Уведомления: <b>{'включены' if state['notify'] else 'выключены'}</b>\n\n"
        f"<i>v{VERSION}</i>"
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
        f"{head('📱', 'Номера')}\n"
        f"{stock_icon(free, total)} Свободно: <b>{free}</b>\nВыдано: <b>{total - free}</b>\nВсего: <b>{total}</b>\n"
        f"{bar(free, total)}\n\n"
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
    pages = max(1, math.ceil(free / per_page))
    page = max(0, min(page, pages - 1))
    rows = db.execute(
        "SELECT id, number FROM numbers WHERE used=0 ORDER BY id LIMIT ? OFFSET ?",
        (per_page, page * per_page)).fetchall()
    body = "\n".join(f"<code>{i}</code> · {esc(n)}" for i, n in rows) or "<i>Свободных номеров нет.</i>"
    return (f"{head('📋', 'Номера', 'Свободные')}  ({free})\n{body}",
            kb([pager("free", page, pages), [button("⬅️ К номерам", "numbers")]]))


def history_view(page=0):
    total = count("SELECT COUNT(*) FROM numbers WHERE used=1")
    per_page = 12
    pages = max(1, math.ceil(total / per_page))
    page = max(0, min(page, pages - 1))
    rows = db.execute("""
        SELECT id, number, used_at, used_by_name FROM numbers
        WHERE used=1 ORDER BY used_at DESC, id DESC LIMIT ? OFFSET ?
    """, (per_page, page * per_page)).fetchall()

    body = "\n".join(
        f"<code>#{nid}</code> · {esc(number)} · {esc(short(who or '—', 16))} · {when(ts)}"
        for nid, number, ts, who in rows
    ) or "<i>Выдач пока не было.</i>"
    if rows:
        body += "\n\n<i>↩️ — вернуть номер в свободные</i>"

    restore = [button(f"↩️ {nid}", f"restore:{nid}:{page}") for nid, *_ in rows]
    keyboard = [restore[i:i + 4] for i in range(0, len(restore), 4)]
    keyboard += [pager("hist", page, pages), [button("⬅️ К номерам", "numbers")]]
    return f"{head('📜', 'Номера', 'История')}  ({total})\n{body}", kb(keyboard)


def clear_view():
    used = count("SELECT COUNT(*) FROM numbers WHERE used=1")
    return (
        f"{head('🧹', 'Номера', 'Очистка')}\nБудет удалено номеров: <b>{used}</b>.\n"
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
    return (f"{head('💬', 'Чаты')}\n{hint}\n\n"
            "<i>Новый чат: добавь бота в группу и отправь там</i> <code>/addchat</code>",
            kb(keyboard))


def keywords_view():
    rows = db.execute("SELECT id, word FROM keywords ORDER BY id").fetchall()
    keyboard = [[button(f"🗑 {short(word)}", f"kwdel:{kid}")] for kid, word in rows]
    keyboard += [[button("➕ Добавить слово", "kwadd")], [button("⬅️ В меню", "home")]]
    words = ", ".join(f"<code>{esc(w)}</code>" for _, w in rows) or "<i>нет</i>"
    return (f"{head('🔑', 'Слова')}\n{words}\n\n"
            f"<i>Опечатки и формы слов («слетт», «номерок») бот узнаёт сам.\n"
            f"Сообщения длиннее {MAX_WORDS} слов игнорируются, кроме «дай(те) номер».\n"
            "Нажми на слово, чтобы удалить его.</i>", kb(keyboard))


def week_chart():
    """Выдачи по дням за неделю символами ▁▃▅▇. Возвращает (текст, сегодня, за неделю)."""
    days = [date.today() - timedelta(days=i) for i in range(6, -1, -1)]
    since = datetime.combine(days[0], datetime.min.time()).timestamp()
    counts = dict.fromkeys(days, 0)
    for (ts,) in db.execute("SELECT used_at FROM numbers WHERE used=1 AND used_at>=?", (since,)):
        day = date.fromtimestamp(ts)
        if day in counts:
            counts[day] += 1
    values = list(counts.values())
    top = max(values)
    bars = "".join(f" {SPARK[max(0, math.ceil(v / top * 8) - 1)] if v else '·'} " for v in values)
    labels = "".join(f"{WEEKDAYS[d.weekday()]} " for d in days)
    return f"{bars}\n{labels}", values[-1], sum(values)


def stats_view():
    total, free = number_counts()
    chart, today, week = week_chart()
    top = db.execute("""
        SELECT COALESCE(c.title, n.used_chat), COUNT(*) AS cnt
        FROM numbers n LEFT JOIN chats c ON c.chat_id = n.used_chat
        WHERE n.used=1 AND n.used_chat IS NOT NULL
        GROUP BY n.used_chat ORDER BY cnt DESC LIMIT 3
    """).fetchall()
    top_text = "\n".join(
        f"  {i}. {esc(short(t, 26))} — <b>{n}</b>" for i, (t, n) in enumerate(top, 1)) or "  —"
    text = (
        f"{head('📊', 'Статистика')}\n"
        f"Состояние: {'🟢 включён' if state['enabled'] else '🔴 выключен'}\n\n"
        f"📱 Всего номеров: <b>{total}</b>\n{stock_icon(free, total)} Свободно: <b>{free}</b>\n"
        f"📤 Выдано: <b>{total - free}</b>\n{bar(free, total)}\n\n"
        f"📅 Сегодня: <b>{today}</b>   🗓 За 7 дней: <b>{week}</b>\n<code>{chart}</code>\n\n"
        f"🏆 <b>Топ чатов</b>\n{top_text}"
    )
    return text, kb([[button("🔄 Обновить", "stats"), button("⬅️ В меню", "home")]])


# --------------------------------------------------------------------- Команды

def admin_private(m: Message):
    return bool(m.from_user) and is_admin(m.from_user.id) and m.chat.type == "private"


@dp.message(Command("start", "panel", "menu"))
async def start(m: Message):
    if m.chat.type != "private":
        return
    if m.from_user and is_admin(m.from_user.id):
        uid = m.from_user.id
        pending.pop(uid, None)
        spawn(drop(m.chat.id, m.message_id))
        await show_panel(uid, panel_view(), fresh=True)
    else:
        await m.answer("Бот работает.")


@dp.message(Command("cancel"))
async def cancel(m: Message):
    if not admin_private(m):
        return
    pending.pop(m.from_user.id, None)
    spawn(drop(m.chat.id, m.message_id))
    await show_panel(m.from_user.id, panel_view(), "✖️ Действие отменено.")


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
    spawn(drop(m.chat.id, m.message_id))
    parts = m.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        note = "ℹ️ <code>/add +79990000000</code>, список — каждый номер с новой строки."
    else:
        added, skipped = add_numbers(parts[1].splitlines())
        note = f"✅ Добавлено: <b>{added}</b> · пропущено: <b>{skipped}</b>"
    await show_panel(m.from_user.id, numbers_view(), note)


@dp.message(Command("delete"))
async def delete_number(m: Message):
    if not admin_private(m):
        return
    spawn(drop(m.chat.id, m.message_id))
    parts = m.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().isdigit():
        note = "ℹ️ <code>/delete ID</code>"
    else:
        cur = db.execute("DELETE FROM numbers WHERE id=?", (int(parts[1]),))
        db.commit()
        note = "✅ Удалено." if cur.rowcount else "⚠️ Номер не найден."
    await show_panel(m.from_user.id, numbers_view(), note)


# -------------------------------------------------------------- Кнопки (меню)

@dp.callback_query()
async def callbacks(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True)
        return
    if c.message is None or not c.data:
        await c.answer()
        return

    uid, data, toast = c.from_user.id, c.data, None
    if data != "noop":
        now = time.monotonic()
        key = (uid, c.message.message_id, data)
        if now - tap_times.get(key, float("-inf")) < TAP_GUARD:  # двойной тап
            await c.answer()
            return
        tap_times[key] = now
        pending.pop(uid, None)  # любая навигация отменяет ожидание ввода
    if not data.endswith(":n"):  # «:n» — кнопка под уведомлением, это не меню
        panel_msg[uid] = c.message.message_id

    if data == "home":
        await safe_edit(c, *panel_view())

    elif data == "bot:toggle":
        set_setting("enabled", "0" if state["enabled"] else "1")
        await safe_edit(c, *panel_view())
        toast = "Выдача включена" if state["enabled"] else "Выдача выключена"

    elif data == "notify:toggle":
        set_setting("notify", "0" if state["notify"] else "1")
        await safe_edit(c, *panel_view())
        toast = "Уведомления включены" if state["notify"] else "Уведомления выключены"

    elif data == "numbers":
        await safe_edit(c, *numbers_view())

    elif data == "numadd":
        pending[uid] = "numbers"
        await safe_edit(
            c, f"{head('➕', 'Номера', 'Добавить')}\nОтправь один номер или список — "
               "каждый с новой строки.\nПовторы бот пропустит сам.",
            kb([[button("✖️ Отмена", "numbers")]]))

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
                await safe_edit(c, c.message.html_text + "\n\n↩️ <i>Номер возвращён в свободные</i>", None)
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
        db.execute("UPDATE chats SET enabled=1-enabled WHERE chat_id=?", (int(data.split(":")[1]),))
        db.commit()
        reload_chats()
        await safe_edit(c, *chats_view())

    elif data.startswith("remove:"):
        db.execute("DELETE FROM chats WHERE chat_id=?", (int(data.split(":")[1]),))
        db.commit()
        reload_chats()
        await safe_edit(c, *chats_view())
        toast = "Чат отключён"

    elif data == "keywords":
        await safe_edit(c, *keywords_view())

    elif data == "kwadd":
        pending[uid] = "keyword"
        await safe_edit(c, f"{head('✏️', 'Слова', 'Добавить')}\nОтправь ключевое слово или фразу.",
                        kb([[button("✖️ Отмена", "keywords")]]))

    elif data.startswith("kwdel:"):
        db.execute("DELETE FROM keywords WHERE id=?", (int(data.split(":")[1]),))
        db.commit()
        reload_keywords()
        await safe_edit(c, *keywords_view())

    elif data == "stats":
        await safe_edit(c, *stats_view())

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
    uid = m.from_user.id
    kind = pending.pop(uid, None)
    spawn(drop(m.chat.id, m.message_id))  # убираем сообщение админа, чтобы чат был чистым

    if kind == "numbers":
        added, skipped = add_numbers(m.text.splitlines())
        await show_panel(uid, numbers_view(),
                         f"✅ Добавлено: <b>{added}</b> · пропущено: <b>{skipped}</b>")
        return

    word = m.text.strip()
    if not word or len(word) > 60:
        await show_panel(uid, keywords_view(), "⚠️ Слово пустое или слишком длинное.")
        return
    try:
        db.execute("INSERT INTO keywords (word) VALUES (?)", (word,))
        db.commit()
    except sqlite3.IntegrityError:
        await show_panel(uid, keywords_view(), "⚠️ Такое слово уже есть.")
        return
    reload_keywords()
    await show_panel(uid, keywords_view(), f"✅ Добавлено: <code>{esc(word)}</code>")


# ------------------------------------------------ Уведомления админу и ошибки

async def notify_admin(m: Message, number_id, number):
    try:
        user = m.from_user
        username = f" (@{esc(user.username)})" if user.username else ""
        total, free = number_counts()
        lines = [
            head("🔔", "Выдан номер"),
            f"📱 <code>{esc(number)}</code>",
            f'👤 <a href="tg://user?id={user.id}">{esc(user.full_name)}</a>{username}',
            f"💬 {esc(m.chat.title or m.chat.id)}",
            f"✉️ <i>{esc(short(m.text, 80))}</i>",
            "", f"{stock_icon(free, total)} Осталось свободных: <b>{free}</b> из {total}", bar(free, total),
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


async def report_chat_error(chat, exc):
    """Одна строка админу, если бот не может писать в чат. Повторы по тому же чату — раз в час."""
    if not isinstance(exc, (TelegramForbiddenError, TelegramBadRequest)):
        return
    now = time.monotonic()
    if now - chat_errors.get(chat.id, float("-inf")) < ERROR_COOLDOWN:
        return
    chat_errors[chat.id] = now
    err = str(exc).lower()
    title = esc(chat.title or chat.id)
    if any(k in err for k in ("kicked", "not a member", "chat not found", "deactivated")):
        line = f"⚠️ Бота удалили из чата «{title}»."
    elif any(k in err for k in ("rights", "write_forbidden", "not enough", "restricted", "can't send")):
        line = f"⚠️ В чате «{title}» нет прав писать."
    else:
        line = f"⚠️ В чате «{title}» не удалось отправить сообщение: {esc(short(exc, 80))}"
    try:
        await bot.send_message(ADMIN_ID, line)
    except Exception:
        log.exception("Не удалось отправить админу сообщение об ошибке")


# ---------------------------------------------------------- Автоматическая выдача

async def handle_empty(m: Message):
    now = time.monotonic()
    if now - empty_notice.get(m.chat.id, float("-inf")) > EMPTY_NOTICE_COOLDOWN:
        empty_notice[m.chat.id] = now
        try:
            await m.reply("😔 Свободные номера закончились.")
        except Exception as e:
            log.warning("Не удалось отправить сообщение о конце номеров: %s", e)
            await report_chat_error(m.chat, e)

    if not state["admin_notified"]:
        state["admin_notified"] = True
        try:
            await bot.send_message(
                ADMIN_ID, f"{head('⚠️', 'Номера закончились')}\nДобавь новые: /add")
        except Exception:
            log.exception("Не удалось уведомить админа")


async def react_pause(m: Message):
    """Человек в паузе: вместо молчания ставим реакцию, чтобы было видно, что сообщение замечено."""
    try:
        await bot.set_message_reaction(
            m.chat.id, m.message_id, reaction=[ReactionTypeEmoji(emoji=PAUSE_REACTION)])
    except Exception:
        log.debug("Не удалось поставить реакцию", exc_info=True)


async def after_issue(m: Message, number_id, number, who):
    """Всё, что не нужно для ответа человеку, делаем уже после него."""
    db.commit()
    log.info("Номер %s выдан %s в чате %s", number, who, m.chat.id)
    if state["notify"]:
        await notify_admin(m, number_id, number)


def is_reply_to_other(m: Message):
    r = m.reply_to_message
    if not r or r.forum_topic_created:  # в темах форума ответ на «корень» темы — не ответ человеку
        return False
    return not (r.from_user and r.from_user.id == m.from_user.id)


@dp.message(F.text)
async def auto_reply(m: Message):
    started = time.perf_counter()
    user = m.from_user
    if (m.chat.type == "private" or not user or user.is_bot or m.via_bot
            or m.forward_origin or is_reply_to_other(m)):
        return
    if not state["enabled"] or m.chat.id not in active_chats:
        return

    text = norm(m.text)
    stems = [squeeze(t) for t in re.findall(r"\w+", text)[:MAX_TOKENS]]
    if not phrase_hit(stems, state["keywords"]):
        return
    if len(text.split()) > MAX_WORDS and not phrase_hit(stems, ALLOW_TOKENS):
        return

    now = time.monotonic()
    key = (m.chat.id, user.id)
    if USER_COOLDOWN and not is_admin(user.id):
        last = last_issue.get(key)
        if last is not None and now - last < USER_COOLDOWN:
            spawn(react_pause(m))
            return

    # Между SELECT и UPDATE нет await, поэтому два сообщения не получат один номер.
    row = db.execute("SELECT id, number FROM numbers WHERE used=0 ORDER BY id LIMIT 1").fetchone()
    if not row:
        spawn(handle_empty(m))
        return
    number_id, number = row

    who = f"@{user.username}" if user.username else user.full_name
    db.execute(
        "UPDATE numbers SET used=1, used_at=?, used_by=?, used_by_name=?, used_chat=? WHERE id=?",
        (int(time.time()), user.id, who, m.chat.id, number_id))
    last_issue[key] = now  # ставим до отправки, чтобы быстрый второй запрос не прошёл

    elapsed = max(time.perf_counter() - started, 0.01)
    card = (
        f"📱 <b>Ваш номер</b>\n<code>{esc(number)}</code>\n\n"
        f"👤 {esc(short(user.full_name, 30))}\n"
        f"⚡ выдан за {elapsed:.2f} с"
    )
    copy = kb([[InlineKeyboardButton(text="📋 Скопировать", copy_text=CopyTextButton(text=number))]])
    try:
        await m.reply(card, reply_markup=copy)
    except Exception as e:
        log.exception("Ошибка отправки номера, возвращаю его в свободные")
        db.execute(f"UPDATE numbers SET {FREE_SQL} WHERE id=?", (number_id,))
        db.commit()
        last_issue.pop(key, None)
        spawn(report_chat_error(m.chat, e))
        return

    spawn(after_issue(m, number_id, number, who))


# ------------------------------------------------------------- Очистка памяти

async def janitor():
    """Раз в несколько минут выбрасывает из словарей устаревшие записи."""
    stores = ((last_issue, USER_COOLDOWN), (empty_notice, EMPTY_NOTICE_COOLDOWN),
              (chat_errors, ERROR_COOLDOWN), (tap_times, TAP_GUARD))
    while True:
        await asyncio.sleep(CLEANUP_EVERY)
        now = time.monotonic()
        for store, ttl in stores:
            for k in [k for k, t in store.items() if now - t > ttl]:
                del store[k]


# ----------------------------------------------------------------------- Запуск

async def main():
    reload_keywords()
    reload_settings()
    reload_chats()
    cleaner = asyncio.create_task(janitor())
    log.info("NUMBER BOT %s запущен!", VERSION)
    try:
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
    finally:
        cleaner.cancel()
        await bot.session.close()
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
