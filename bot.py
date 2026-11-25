import asyncio
import logging
import os

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

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; NewsBot/1.0)"}

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

telegraph_token = TELEGRAPH_TOKEN
# Кэш: ссылка на новость -> адрес готовой статьи Telegraph, чтобы не создавать дубли
telegraph_cache: dict[str, str] = {}


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


def text_to_nodes(text: str, source: str) -> list:
    nodes = []
    text = text[:30000]  # у Telegraph ограничение на размер статьи
    for paragraph in text.split("\n"):
        paragraph = paragraph.strip()
        if paragraph:
            nodes.append({"tag": "p", "children": [paragraph]})
    nodes.append({"tag": "p", "children": [{"tag": "i", "children": [f"Источник: {source}"]}]})
    return nodes


async def make_article(session: aiohttp.ClientSession, entry, source: str) -> str:
    link = entry.link
    if link in telegraph_cache:
        return telegraph_cache[link]

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
        text = trafilatura.extract(f"<html><body><p>{summary}</p></body></html>") or summary or entry.title

    token = await ensure_telegraph_account(session)
    page = await telegraph_call(session, "createPage", {
        "access_token": token,
        "title": entry.title[:256],
        "author_name": source,
        "content": text_to_nodes(text, source),
    })
    telegraph_cache[link] = page["url"]
    return page["url"]


# ---------- Новости ----------

SOURCE_NAMES = {
    "Главные новости": "Лента.ру",
    "Технологии": "Хабр",
}


async def collect_articles() -> list[tuple[str, str, str]]:
    """Возвращает список (категория, заголовок, ссылка на Telegraph)."""
    articles = []
    async with aiohttp.ClientSession() as session:
        for category, url in RSS_FEEDS.items():
            feed = await asyncio.to_thread(feedparser.parse, url)
            for entry in feed.entries[:NEWS_PER_SOURCE]:
                try:
                    page_url = await make_article(session, entry, SOURCE_NAMES.get(category, category))
                    articles.append((category, entry.title, page_url))
                except Exception as e:
                    logging.error(f"Не удалось сделать статью для {entry.get('link')}: {e}")
    return articles


def escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "Привет! Я собираю свежие новости и присылаю их статьями, "
        "которые можно читать прямо в Telegram.\n\n"
        "/news — получить свежую сводку",
        parse_mode="HTML"
    )


@dp.message(Command("news"))
async def cmd_news(message: types.Message):
    await message.answer("Собираю свежие новости...")
    articles = await collect_articles()

    if not articles:
        await message.answer("Не удалось загрузить новости. Попробуй чуть позже.")
        return

    current_category = None
    for category, title, page_url in articles:
        header = f"<b>— {category} —</b>\n\n" if category != current_category else ""
        current_category = category
        # Заголовок скрыто ведёт на статью Telegraph: в превью появится кнопка «Мгновенный просмотр»
        await message.answer(
            f"{header}<a href='{page_url}'>{escape(title)}</a>",
            parse_mode="HTML",
        )


async def main():
    if not BOT_TOKEN:
        raise ValueError("Не задана переменная окружения BOT_TOKEN!")

    logging.info("Бот запущен!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
