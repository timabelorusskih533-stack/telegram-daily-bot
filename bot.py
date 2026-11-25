import asyncio
import json
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
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
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
        logging.info("Создан аккаунт Telegraph")
    return telegraph_token


MAX_CONTENT_BYTES = 40000  # у Telegraph лимит 64 КБ на статью, берём с запасом


def paragraphs(text: str, limit: int) -> list:
    if len(text) > limit:
        cut = text[:limit]
        # Обрезаем по концу предложения, чтобы не обрывать на полуслове
        end = max(cut.rfind(". "), cut.rfind(".\n"))
        text = (cut[:end + 1] if end > limit // 2 else cut) + " …"
    nodes = []
    for paragraph in text.split("\n"):
        paragraph = paragraph.strip()
        if paragraph:
            nodes.append({"tag": "p", "children": [paragraph]})
    return nodes


def build_content(sections: list, limit: int) -> list:
    content = []
    for category, items in sections:
        content.append({"tag": "h3", "children": [category]})
        for title, text in items:
            content.append({"tag": "h4", "children": [title]})
            content.extend(paragraphs(text, limit))
        content.append({"tag": "p", "children": [
            {"tag": "i", "children": [f"Источник: {SOURCE_NAMES.get(category, category)}"]}
        ]})
        content.append({"tag": "hr"})
    return content


def content_size(content: list) -> int:
    return len(json.dumps(content, ensure_ascii=False).encode("utf-8"))


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
    sections = []
    async with aiohttp.ClientSession() as session:
        for category, url in RSS_FEEDS.items():
            feed = await asyncio.to_thread(feedparser.parse, url)
            items = []
            for entry in feed.entries[:NEWS_PER_SOURCE]:
                text = await get_full_text(session, entry)
                items.append((entry.title, text))
            if items:
                sections.append((category, items))

        if not sections:
            return None

        # Сокращаем тексты, пока статья не уложится в лимит Telegraph
        limit = 6000
        content = build_content(sections, limit)
        while content_size(content) > MAX_CONTENT_BYTES and limit > 300:
            limit = int(limit * 0.8)
            content = build_content(sections, limit)

        token = await ensure_telegraph_account(session)
        title = f"Сводка новостей — {datetime.now(MOSCOW).strftime('%d.%m, %H:%M')}"
        while True:
            try:
                page = await telegraph_call(session, "createPage", {
                    "access_token": token,
                    "title": title,
                    "author_name": "botkaracikov",
                    "content": content,
                })
                break
            except RuntimeError as e:
                # Если всё равно слишком большая — сокращаем ещё вдвое и пробуем снова
                if "CONTENT_TOO_BIG" not in str(e) or limit <= 300:
                    raise
                limit //= 2
                content = build_content(sections, limit)
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
