import asyncio
import html as html_lib
import json
import logging
import os
import re
import uuid
from datetime import date, datetime, timedelta, timezone

import aiohttp
import feedparser
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.types import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from schedule import SITE_URL, Lesson, parse_schedule

# Токен подтянется из настроек хостинга (Environment Variables)
BOT_TOKEN = os.getenv("BOT_TOKEN")
# Необязательно: постоянный токен Telegraph-аккаунта. Если не задан — создаётся при запуске.
TELEGRAPH_TOKEN = os.getenv("TELEGRAPH_TOKEN")

MOSCOW = timezone(timedelta(hours=3))
EVENING_HOUR = 20          # во сколько присылать расписание на завтра
REMIND_BEFORE_MIN = 15     # за сколько минут напоминать о паре
SCHEDULE_REFRESH_MIN = 30  # как часто перечитывать сайт с расписанием
NEWS_TIMES = ["07:30", "14:00", "17:30", "22:00"]  # когда присылать сводку новостей
EXAM_KINDS = {"Экзамен", "Зачёт", "Зачёт (письменный)", "Дифференцированный зачёт", "Контрольная работа"}

# Данные пользователей храним в JSON. На Bothost папка /app/shared не стирается при обновлении бота.
DATA_DIR = os.getenv("DATA_DIR") or ("/app/shared" if os.path.isdir("/app/shared") else "data")
DATA_FILE = os.path.join(DATA_DIR, "botkaracikov_data.json")

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


def now_msk() -> datetime:
    return datetime.now(MOSCOW).replace(tzinfo=None)


# ======================= Хранилище =======================

NOTIFY_DEFAULTS = {"evening": True, "before": True, "otr": True, "changes": True, "news": True}
NOTIFY_NAMES = {
    "evening": f"Расписание на завтра в {EVENING_HOUR}:00",
    "before": f"За {REMIND_BEFORE_MIN} минут до пары",
    "otr": "Незакрытые отработки",
    "changes": "Изменения в расписании",
    "news": "Новости в " + ", ".join(NEWS_TIMES),
}

data: dict = {"users": {}, "sent": []}


def load_data():
    global data
    # Если общее хранилище подключили позже — переносим данные из старой папки
    for path in (DATA_FILE, os.path.join("data", "botkaracikov_data.json")):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            break
        except FileNotFoundError:
            continue
        except Exception as e:
            logging.error(f"Не удалось прочитать {path}: {e}")
    data.setdefault("users", {})
    data.setdefault("sent", [])
    logging.info(f"Данные: {DATA_FILE}, пользователей: {len(data['users'])}")
    save_data()


def save_data():
    os.makedirs(DATA_DIR, exist_ok=True)
    data["sent"] = data["sent"][-500:]
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, DATA_FILE)


def get_user(uid: int) -> dict:
    user = data["users"].setdefault(str(uid), {})
    user.setdefault("notify", dict(NOTIFY_DEFAULTS))
    for k, v in NOTIFY_DEFAULTS.items():
        user["notify"].setdefault(k, v)
    user.setdefault("otr", [])
    return user


# ======================= Расписание =======================

lessons: list[Lesson] = []
schedule_loaded_at: datetime | None = None
DOW = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
DOW_S = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
MON = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
       "августа", "сентября", "октября", "ноября", "декабря"]


async def fetch_schedule() -> list[Lesson]:
    async with aiohttp.ClientSession() as session:
        async with session.get(SITE_URL, params={"t": int(datetime.now().timestamp())},
                               timeout=aiohttp.ClientTimeout(total=30)) as resp:
            html = await resp.text()
    return parse_schedule(html)


def lessons_on(day: date) -> list[Lesson]:
    return [x for x in lessons if x.day == day]


def day_title(day: date) -> str:
    return f"{DOW[day.weekday()].capitalize()}, {day.day} {MON[day.month - 1]}"


def esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n) % 100
    if 11 <= n <= 14:
        return many
    d = n % 10
    if d == 1:
        return one
    if 2 <= d <= 4:
        return few
    return many


def lesson_line(x: Lesson, with_topic: bool = True) -> str:
    mark = "🔴 " if x.kind in EXAM_KINDS else "🕘 "
    rows = [f"{mark}<b>{x.start}–{x.end}</b> · {esc(x.kind)}",
            f"<b>{esc(x.subject)}</b>",
            f"👤 {esc(x.teacher)}"]
    if with_topic and x.topic:
        rows.append(f"<i>{esc(x.topic)}</i>")
    return "<blockquote>" + "\n".join(rows) + "</blockquote>"


def day_text(day: date, with_topic: bool = True) -> str:
    items = lessons_on(day)
    head = f"📅 <b>{day_title(day)}</b>"
    if not items:
        return f"{head}\n\nПар нет 🎉"
    tail = f"\n\n{len(items)} " + plural(len(items), "пара", "пары", "пар")
    return head + "\n\n" + "\n".join(lesson_line(x, with_topic) for x in items) + tail


def upcoming_exams(days: int = 7) -> list[Lesson]:
    today = now_msk().date()
    return [x for x in lessons if x.kind in EXAM_KINDS and today <= x.day <= today + timedelta(days=days)]


# ======================= Отработки =======================

def open_otr(user: dict) -> list[dict]:
    return [o for o in user["otr"] if not o.get("done")]


def otr_line(o: dict) -> str:
    d = date.fromisoformat(o["d"])
    return ("<blockquote>"
            f"📌 <b>{d.day} {MON[d.month - 1]}</b> · {o['t']}\n"
            f"<b>{esc(o['subj'])}</b>\n"
            f"{esc(o['kind'])}"
            "</blockquote>")


def otr_view(user: dict) -> tuple[str, InlineKeyboardMarkup]:
    opened = open_otr(user)
    done = [o for o in user["otr"] if o.get("done")]
    if opened:
        n = len(opened)
        text = (f"📝 <b>Отработки</b>\nНужно отработать: <b>{n}</b> "
                + plural(n, "пара", "пары", "пар")
                + "\n\n" + "\n".join(otr_line(o) for o in opened))
    elif done:
        text = "📝 <b>Отработки</b>\n\n✅ Всё отработано, долгов нет 🎉"
    else:
        text = ("📝 <b>Отработки</b>\n\nПропусков нет. "
                "Нажми «Добавить пропуск», чтобы отметить пропущенную пару.")
    kb = [[InlineKeyboardButton(text=f"✓ Отработал: {o['subj'][:25]} · {o['d'][8:]}.{o['d'][5:7]}",
                                callback_data=f"od:{o['id']}")] for o in opened]
    kb.append([InlineKeyboardButton(text="➕ Добавить пропуск", callback_data="oa")])
    if done:
        kb.append([InlineKeyboardButton(text="↩️ Вернуть отработанную", callback_data="ou")])
    return text, InlineKeyboardMarkup(inline_keyboard=kb)


# ======================= Новости (Telegraph) =======================

RSS_FEEDS = [
    "https://lenta.ru/rss/news",
    "https://habr.com/ru/rss/news/?fl=ru",
]
NEWS_COUNT = 10            # сколько новостей в сводке
SUMMARY_MAX_CHARS = 220    # длина краткого описания
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; NewsBot/1.0)"}

telegraph_token = TELEGRAPH_TOKEN


async def telegraph_call(session: aiohttp.ClientSession, method: str, payload: dict) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    async with session.post(f"https://api.telegra.ph/{method}", data=body,
                            headers={"Content-Type": "application/json"}) as resp:
        result = await resp.json()
    if not result.get("ok"):
        raise RuntimeError(f"Telegraph {method}: {result.get('error')}")
    return result["result"]


async def ensure_telegraph_account(session: aiohttp.ClientSession) -> str:
    global telegraph_token
    if not telegraph_token:
        account = await telegraph_call(session, "createAccount", {
            "short_name": "botkaracikov",
            "author_name": "Сводка новостей",
        })
        telegraph_token = account["access_token"]
    return telegraph_token


def clean_text(raw: str) -> str:
    text = html_lib.unescape(re.sub(r"<[^>]+>", " ", raw or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return re.sub(r"\s*Читать далее.*$", "", text)


def shorten(text: str) -> str:
    """Оставляет 1–2 предложения, не длиннее SUMMARY_MAX_CHARS."""
    sentences = re.split(r"(?<=[.!?…])\s+(?=[А-ЯЁA-Z«\"0-9])", text)
    text = " ".join(sentences[:2])
    if len(text) > SUMMARY_MAX_CHARS:
        first = sentences[0]
        text = first if len(first) <= SUMMARY_MAX_CHARS else first[:SUMMARY_MAX_CHARS].rsplit(" ", 1)[0] + "…"
    return text


META_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\'](?:og:description|description)["\'][^>]*>', re.I)
CONTENT_RE = re.compile(r'content=["\']([^"\']*)["\']', re.I)


async def page_description(session: aiohttp.ClientSession, url: str) -> str:
    """Краткое описание из самой статьи (meta description)."""
    try:
        async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            html = await resp.text()
    except Exception as e:
        logging.warning(f"Не удалось открыть {url}: {e}")
        return ""
    for tag in META_RE.findall(html):
        m = CONTENT_RE.search(tag)
        if m and m.group(1).strip():
            return clean_text(m.group(1))
    return ""


async def describe(session: aiohttp.ClientSession, entry) -> str:
    title = entry.title.strip()
    text = clean_text(entry.get("summary", ""))
    if not text or text == title:
        text = await page_description(session, entry.link)
    return shorten(text) if text and text != title else ""


async def make_digest() -> str | None:
    """10 самых свежих новостей: заголовок и 1–2 предложения, одной статьёй Telegraph."""
    entries = []
    for url in RSS_FEEDS:
        feed = await asyncio.to_thread(feedparser.parse, url)
        entries += feed.entries
    if not entries:
        return None
    entries.sort(key=lambda e: tuple(e.get("published_parsed") or (0,) * 9), reverse=True)
    entries = entries[:NEWS_COUNT]

    async with aiohttp.ClientSession() as session:
        summaries = await asyncio.gather(*(describe(session, e) for e in entries))
        content = []
        for e, summary in zip(entries, summaries):
            content.append({"tag": "h4", "children": [e.title.strip()]})
            if summary:
                content.append({"tag": "p", "children": [summary]})

        token = await ensure_telegraph_account(session)
        page = await telegraph_call(session, "createPage", {
            "access_token": token,
            "title": f"Сводка новостей — {now_msk().strftime('%d.%m, %H:%M')}",
            "author_name": "botkaracikov",
            "content": content,
        })
    return page["url"]


# ======================= Кнопки =======================

BTN_OTR = "📝 Отработки"
BTN_NEWS = "📰 Новости"
BTN_SETTINGS = "🔔 Уведомления"

MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_OTR), KeyboardButton(text=BTN_NEWS), KeyboardButton(text=BTN_SETTINGS)],
    ],
    resize_keyboard=True,
    is_persistent=True,
)


def settings_view(user: dict) -> tuple[str, InlineKeyboardMarkup]:
    kb = [[InlineKeyboardButton(text=("✅ " if user["notify"][k] else "❌ ") + name, callback_data=f"st:{k}")]
          for k, name in NOTIFY_NAMES.items()]
    text = "🔔 <b>Уведомления</b>\nНажми, чтобы включить или выключить:"
    return text, InlineKeyboardMarkup(inline_keyboard=kb)


# ======================= Команды и кнопки =======================

async def ensure_schedule():
    global lessons, schedule_loaded_at
    if not lessons:
        lessons = await fetch_schedule()
        schedule_loaded_at = now_msk()


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    get_user(message.from_user.id)
    save_data()
    await message.answer(
        "Привет! Я присылаю уведомления:\n"
        f"• вечером в {EVENING_HOUR}:00 — пары на завтра и ближайшие зачёты;\n"
        f"• за {REMIND_BEFORE_MIN} минут до каждой пары;\n"
        "• про незакрытые отработки;\n"
        "• если расписание на сайте изменилось;\n"
        f"• сводку новостей в {', '.join(NEWS_TIMES)}.\n\n"
        "Кнопки внизу — отработки, новости и настройка уведомлений.",
        reply_markup=MAIN_KB,
    )


@dp.message(Command("otr"))
@dp.message(F.text == BTN_OTR)
async def show_otr(message: types.Message):
    text, kb = otr_view(get_user(message.from_user.id))
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@dp.callback_query(F.data == "oa")
async def cb_otr_add(call: types.CallbackQuery):
    """Шаг 1: выбор дня, в который был пропуск (последние 14 дней с парами)."""
    await ensure_schedule()
    today = now_msk().date()
    days = sorted({x.day for x in lessons if today - timedelta(days=14) <= x.day <= today}, reverse=True)
    if not days:
        await call.answer("За последние две недели пар не было", show_alert=True)
        return
    kb = [[InlineKeyboardButton(text=f"{DOW_S[d.weekday()]}, {d.day} {MON[d.month - 1]}",
                                callback_data=f"oad:{d.isoformat()}")] for d in days]
    kb.append([InlineKeyboardButton(text="« Назад", callback_data="ob")])
    await call.message.edit_text("В какой день был пропуск?", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await call.answer()


@dp.callback_query(F.data.startswith("oad:"))
async def cb_otr_add_day(call: types.CallbackQuery):
    """Шаг 2: выбор пары в этот день."""
    day = date.fromisoformat(call.data[4:])
    kb = [[InlineKeyboardButton(text=f"{x.start} {x.subject[:30]} ({x.kind[:12]})",
                                callback_data=f"oal:{day.isoformat()}|{x.start}")] for x in lessons_on(day)]
    kb.append([InlineKeyboardButton(text="« Назад", callback_data="oa")])
    await call.message.edit_text(f"Какую пару пропустил — {day_title(day)}?",
                                 reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await call.answer()


@dp.callback_query(F.data.startswith("oal:"))
async def cb_otr_add_lesson(call: types.CallbackQuery):
    d, t = call.data[4:].split("|")
    lesson = next((x for x in lessons_on(date.fromisoformat(d)) if x.start == t), None)
    if not lesson:
        await call.answer("Пара не найдена", show_alert=True)
        return
    user = get_user(call.from_user.id)
    if not any(o["d"] == d and o["t"] == t for o in user["otr"]):
        user["otr"].append({"id": uuid.uuid4().hex[:8], "d": d, "t": t, "subj": lesson.subject,
                            "kind": lesson.kind, "teacher": lesson.teacher, "done": False})
        user["otr"].sort(key=lambda o: (o["d"], o["t"]))
        save_data()
    text, kb = otr_view(user)
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await call.answer("Пропуск добавлен")


@dp.callback_query(F.data.startswith("od:"))
async def cb_otr_done(call: types.CallbackQuery):
    user = get_user(call.from_user.id)
    for o in user["otr"]:
        if o["id"] == call.data[3:]:
            o["done"] = True
            o["done_date"] = now_msk().date().isoformat()
    save_data()
    text, kb = otr_view(user)
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await call.answer("Отмечено как отработанное ✅")


@dp.callback_query(F.data == "ou")
async def cb_otr_undo_list(call: types.CallbackQuery):
    user = get_user(call.from_user.id)
    done = [o for o in user["otr"] if o.get("done")]
    kb = [[InlineKeyboardButton(text=f"↩️ {o['subj'][:28]} {o['d'][8:]}.{o['d'][5:7]}",
                                callback_data=f"ouu:{o['id']}")] for o in done]
    kb.append([InlineKeyboardButton(text="« Назад", callback_data="ob")])
    await call.message.edit_text("Какую вернуть в «нужно отработать»?",
                                 reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await call.answer()


@dp.callback_query(F.data.startswith("ouu:"))
async def cb_otr_undo(call: types.CallbackQuery):
    user = get_user(call.from_user.id)
    for o in user["otr"]:
        if o["id"] == call.data[4:]:
            o["done"] = False
            o.pop("done_date", None)
    save_data()
    text, kb = otr_view(user)
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await call.answer()


@dp.callback_query(F.data == "ob")
async def cb_otr_back(call: types.CallbackQuery):
    text, kb = otr_view(get_user(call.from_user.id))
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await call.answer()


@dp.message(Command("settings"))
@dp.message(F.text == BTN_SETTINGS)
async def show_settings(message: types.Message):
    text, kb = settings_view(get_user(message.from_user.id))
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@dp.callback_query(F.data.startswith("st:"))
async def cb_settings(call: types.CallbackQuery):
    user = get_user(call.from_user.id)
    k = call.data[3:]
    if k in user["notify"]:
        user["notify"][k] = not user["notify"][k]
        save_data()
    text, kb = settings_view(user)
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await call.answer()


@dp.message(Command("news"))
@dp.message(F.text == BTN_NEWS)
async def cmd_news(message: types.Message):
    status = await message.answer("Собираю свежие новости...")
    try:
        page_url = await make_digest()
    except Exception as e:
        logging.error(f"Не удалось собрать сводку: {e}")
        page_url = None
    if not page_url:
        await status.edit_text("Не удалось загрузить новости. Попробуй чуть позже.")
        return
    # Одно сообщение: заголовок ведёт на статью Telegraph, в превью — кнопка «Посмотреть»
    await status.edit_text(f"📰 <a href='{page_url}'>Сводка новостей</a>", parse_mode="HTML")


# ======================= Фоновые уведомления =======================

def already_sent(key: str) -> bool:
    if key in data["sent"]:
        return True
    data["sent"].append(key)
    return False


async def send_to(uid: str, text: str, preview: bool = False):
    try:
        await bot.send_message(int(uid), text, parse_mode="HTML", disable_web_page_preview=not preview)
    except Exception as e:
        logging.error(f"Не удалось отправить {uid}: {e}")


def evening_text(user: dict) -> str:
    tomorrow = now_msk().date() + timedelta(days=1)
    parts = []
    if user["notify"]["evening"]:
        parts.append("🌙 <b>Пары на завтра</b>\n\n" + day_text(tomorrow))
        exams = [x for x in upcoming_exams(7) if x.day > tomorrow]
        if exams:
            parts.append("❗️ <b>Скоро:</b>\n" + "\n".join(
                f"• {x.day.day} {MON[x.day.month - 1]} — {esc(x.kind)}: {esc(x.subject)}" for x in exams))
    opened = open_otr(user)
    if user["notify"]["otr"] and opened:
        parts.append(f"📝 <b>Незакрытые отработки: {len(opened)}</b>\n" +
                     "\n".join(f"• {otr_line(o)}" for o in opened))
    return "\n\n".join(parts)


def snapshot(items: list[Lesson]) -> dict[str, set]:
    today = now_msk().date()
    snap: dict[str, set] = {}
    for x in items:
        if today <= x.day <= today + timedelta(days=14):
            snap.setdefault(x.day.isoformat(), set()).add((x.start, x.subject, x.kind, x.teacher))
    return snap


def diff_text(old: dict, new: dict) -> str:
    parts = []
    for d in sorted(set(old) | set(new)):
        removed = old.get(d, set()) - new.get(d, set())
        added = new.get(d, set()) - old.get(d, set())
        if not removed and not added:
            continue
        lines = [f"<b>{day_title(date.fromisoformat(d))}</b>"]
        lines += [f"➖ <s>{t} {esc(s)} ({esc(k)})</s>" for t, s, k, _ in sorted(removed)]
        lines += [f"➕ {t} {esc(s)} ({esc(k)}) · {esc(te)}" for t, s, k, te in sorted(added)]
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


async def refresh_schedule():
    """Перечитывает сайт и рассылает изменения на ближайшие 2 недели."""
    global lessons, schedule_loaded_at
    schedule_loaded_at = now_msk()
    try:
        new = await fetch_schedule()
    except Exception as e:
        logging.error(f"Не удалось обновить расписание: {e}")
        return
    if lessons:
        changes = diff_text(snapshot(lessons), snapshot(new))
        if changes:
            for uid in list(data["users"]):
                if get_user(int(uid))["notify"]["changes"]:
                    await send_to(uid, "✏️ <b>Расписание изменилось</b>\n\n" + changes)
    lessons = new


async def send_news_to_all():
    users = [uid for uid in list(data["users"]) if get_user(int(uid))["notify"]["news"]]
    if not users:
        return
    try:
        page_url = await make_digest()
    except Exception as e:
        logging.error(f"Не удалось собрать сводку: {e}")
        return
    if page_url:
        for uid in users:
            await send_to(uid, f"📰 <a href='{page_url}'>Сводка новостей</a>", preview=True)


async def notifier():
    while True:
        try:
            now = now_msk()
            if not schedule_loaded_at or now - schedule_loaded_at >= timedelta(minutes=SCHEDULE_REFRESH_MIN):
                await refresh_schedule()

            changed = False
            # Вечерняя сводка: пары на завтра, ближайшие зачёты, незакрытые отработки
            if now.hour == EVENING_HOUR and not already_sent(f"evening:{now.date()}"):
                changed = True
                for uid in list(data["users"]):
                    text = evening_text(get_user(int(uid)))
                    if text:
                        await send_to(uid, text)

            # Напоминание перед парой
            for x in lessons_on(now.date()):
                delta = x.start_dt - now
                if timedelta(0) < delta <= timedelta(minutes=REMIND_BEFORE_MIN):
                    if already_sent(f"before:{x.day}:{x.start}"):
                        continue
                    changed = True
                    mins = max(1, -(-int(delta.total_seconds()) // 60))  # округляем вверх
                    text = f"⏰ <b>Через {mins} мин</b>\n\n{lesson_line(x)}"
                    for uid in list(data["users"]):
                        if get_user(int(uid))["notify"]["before"]:
                            await send_to(uid, text)
            # Сводка новостей по расписанию (окно 10 минут, чтобы не пропустить при перезапуске)
            for hhmm in NEWS_TIMES:
                h, m = map(int, hhmm.split(":"))
                at = now.replace(hour=h, minute=m, second=0, microsecond=0)
                if at <= now < at + timedelta(minutes=10) and not already_sent(f"news:{now.date()}:{hhmm}"):
                    changed = True
                    save_data()
                    asyncio.create_task(send_news_to_all())

            if changed:
                save_data()
        except Exception as e:
            logging.exception(f"Ошибка в уведомлениях: {e}")
        await asyncio.sleep(30)


async def main():
    if not BOT_TOKEN:
        raise ValueError("Не задана переменная окружения BOT_TOKEN!")

    load_data()
    await refresh_schedule()
    logging.info(f"Расписание загружено: {len(lessons)} пар")

    await bot.set_my_commands([
        BotCommand(command="otr", description="Отработки"),
        BotCommand(command="news", description="Сводка новостей"),
        BotCommand(command="settings", description="Настройка уведомлений"),
    ])

    asyncio.create_task(notifier())
    logging.info("Бот запущен!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
