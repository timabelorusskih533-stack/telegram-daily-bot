import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

import aiohttp
import feedparser
import trafilatura
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command

# Токен подтянется из настроек хостинга (Environment Variables)
BOT_TOKEN = os.getenv("BOT_TOKEN")
# Необязательно: постоянный токен Telegraph-аккаунта. Если не задан — создаётся при запуске.
TELEGRAPH_TOKEN = os.getenv("TELEGRAPH_TOKEN")

RSS_FEEDS = {
    "Главные новости": "https://lenta.ru/rss/news",
    "Технологии": "https://habr.com/ru/rss/news/?fl=ru",
}
NEWS_PER_SOURCE = 3
MOSCOW = timezone(timedelta(hours=3))

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; NewsBot/1.0)"}

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

telegraph_token = TELEGRAPH_TOKEN
# Кэш: ссылка на новость -> полный текст, чтобы не скачивать одно и то же повторно
text_cache: dict[str, str] = {}


# ---------- Telegraph ----------

async def telegraph_call(session: aiohttp.ClientSession, method: str, data: dict) -> dict:
    async with session.post(f"https://api.telegra.ph/{method}", json=data) as resp:
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
        logging.info("Создан аккаунт Telegraph")
    return telegraph_token


# Лимит на одну новость: вся статья Telegraph должна уложиться в 64 КБ
def paragraphs(text: str, limit: int = 4000) -> list:
    nodes = []
    for paragraph in text[:limit].split("\n"):
        paragraph = paragraph.strip()
        if paragraph:
            nodes.append({"tag": "p", "children": [paragraph]})
    return nodes


async def get_full_text(session: aiohttp.ClientSession, entry) -> str:
    link = entry.link
    if link in text_cache:
        return text_cache[link]

    text = None
    try:
        async with session.get(link, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            html = await resp.text()
        text = await asyncio.to_thread(trafilatura.extract, html, include_comments=False, include_tables=False)
    except Exception as e:
        logging.warning(f"Не удалось скачать статью {link}: {e}")

    if not text:
        # Запасной вариант — краткое описание из RSS
        summary = entry.get("summary", "")
        text = trafilatura.extract(f"<html><body><p>{summary}</p></body></html>") or summary or ""

    text_cache[link] = text
    return text


# ---------- Новости ----------

SOURCE_NAMES = {
    "Главные новости": "Лента.ру",
    "Технологии": "Хабр",
}


async def make_digest() -> str | None:
    """Собирает все новости в одну статью Telegraph и возвращает её адрес."""
    content = []
    async with aiohttp.ClientSession() as session:
        for category, url in RSS_FEEDS.items():
            feed = await asyncio.to_thread(feedparser.parse, url)
            entries = feed.entries[:NEWS_PER_SOURCE]
            if not entries:
                continue
            content.append({"tag": "h3", "children": [category]})
            for entry in entries:
                text = await get_full_text(session, entry)
                content.append({"tag": "h4", "children": [entry.title]})
                content.extend(paragraphs(text))
            content.append({"tag": "p", "children": [
                {"tag": "i", "children": [f"Источник: {SOURCE_NAMES.get(category, category)}"]}
            ]})
            content.append({"tag": "hr"})

        if not content:
            return None

        token = await ensure_telegraph_account(session)
        title = f"Сводка новостей — {datetime.now(MOSCOW).strftime('%d.%m, %H:%M')}"
        page = await telegraph_call(session, "createPage", {
            "access_token": token,
            "title": title,
            "author_name": "botkaracikov",
            "content": content,
        })
    return page["url"]


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "Привет! Я собираю свежие новости в одну статью, "
        "которую можно читать прямо в Telegram.\n\n"
        "/news — получить свежую сводку",
        parse_mode="HTML"
    )


@dp.message(Command("news"))
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

    # Одно сообщение: заголовок скрыто ведёт на статью, в превью — кнопка «Посмотреть»
    await status.edit_text(
        f"📰 <a href='{page_url}'>Сводка новостей</a>",
        parse_mode="HTML",
    )


async def main():
    if not BOT_TOKEN:
        raise ValueError("Не задана переменная окружения BOT_TOKEN!")

    logging.info("Бот запущен!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
