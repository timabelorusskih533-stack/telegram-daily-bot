import asyncio
import logging
import os
import feedparser
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# Токен подтянется из настроек хостинга (Environment Variables)
BOT_TOKEN = os.getenv("BOT_TOKEN")
TIMEZONE = "Europe/Moscow"

RSS_FEEDS = {
    "Главные новости": "https://lenta.ru/rss/news",
    "Технологии": "https://habr.com/ru/rss/news/?fl=ru",
}

subscribers = set()

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


def get_latest_news(limit_per_source: int = 3) -> str:
    digest = ["📰 <b>Сводка актуальных новостей:</b>\n"]
    
    for category, url in RSS_FEEDS.items():
        feed = feedparser.parse(url)
        if not feed.entries:
            continue
            
        digest.append(f"<b>— {category} —</b>")
        for entry in feed.entries[:limit_per_source]:
            title = entry.title
            link = entry.link
            digest.append(f"• <a href='{link}'>{title}</a>")
        digest.append("")

    if len(digest) == 1:
        return "Не удалось загрузить новости. Проверьте ссылки на RSS."
        
    return "\n".join(digest)


async def send_scheduled_news(time_of_day: str):
    if not subscribers:
        return

    news_text = get_latest_news()
    header = "☀️ <b>Доброе утро!</b>\n\n" if time_of_day == "morning" else "🌙 <b>Добрый вечер! Итоги дня:</b>\n\n"
    full_message = header + news_text

    for user_id in list(subscribers):
        try:
            await bot.send_message(
                chat_id=user_id,
                text=full_message,
                parse_mode="HTML",
                disable_web_page_preview=True
            )
        except Exception as e:
            logging.error(f"Не удалось отправить сообщение {user_id}: {e}")
            subscribers.discard(user_id)


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    subscribers.add(message.from_user.id)
    await message.answer(
        "Привет! Я подписал тебя на ежедневную сводку новостей.\n"
        "Буду присылать дайджест в <b>08:00</b> и <b>20:00</b>.\n\n"
        "Команды:\n"
        "/news — получить свежую сводку прямо сейчас\n"
        "/stop — отписаться от рассылки",
        parse_mode="HTML"
    )


@dp.message(Command("stop"))
async def cmd_stop(message: types.Message):
    subscribers.discard(message.from_user.id)
    await message.answer("Ты отписался от утренней и вечерней рассылки. Чтобы вернуться, нажми /start.")


@dp.message(Command("news"))
async def cmd_news(message: types.Message):
    await message.answer("Собираю свежие новости...")
    news_text = get_latest_news()
    await message.answer(news_text, parse_mode="HTML", disable_web_page_preview=True)


async def main():
    if not BOT_TOKEN:
        raise ValueError("Не задана переменная окружения BOT_TOKEN!")

    scheduler = AsyncIOScheduler(timezone=TIMEZONE)
    scheduler.add_job(send_scheduled_news, trigger="cron", hour=8, minute=0, args=["morning"])
    scheduler.add_job(send_scheduled_news, trigger="cron", hour=20, minute=0, args=["evening"])
    scheduler.start()
    
    logging.info("Бот запущен!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
