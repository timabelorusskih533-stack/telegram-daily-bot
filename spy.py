"""Отлов удалённых и изменённых сообщений через Telegram Business («Секретарь»).

Telegram при удалении присылает только номера сообщений, поэтому бот
сохраняет копии входящих сообщений из подключённых чатов в SQLite
и хранит их KEEP_DAYS дней.
"""
import html
import logging
import os
import sqlite3
import time

from aiogram import Bot, Router
from aiogram.types import BusinessConnection, BusinessMessagesDeleted, Message

KEEP_DAYS = 7

router = Router()
db: sqlite3.Connection | None = None


def init_db(data_dir: str):
    global db
    os.makedirs(data_dir, exist_ok=True)
    db = sqlite3.connect(os.path.join(data_dir, "messages.db"))
    db.executescript("""
        CREATE TABLE IF NOT EXISTS connections (
            id TEXT PRIMARY KEY, owner_id INTEGER, enabled INTEGER);
        CREATE TABLE IF NOT EXISTS messages (
            conn_id TEXT, chat_id INTEGER, msg_id INTEGER,
            from_id INTEGER, from_name TEXT, chat_name TEXT,
            text TEXT, media_type TEXT, file_id TEXT, saved_at INTEGER,
            PRIMARY KEY (chat_id, msg_id));
        CREATE INDEX IF NOT EXISTS idx_saved ON messages(saved_at);
        CREATE TABLE IF NOT EXISTS caught (
            chat_id INTEGER, msg_id INTEGER, PRIMARY KEY (chat_id, msg_id));
    """)
    db.commit()


def cleanup():
    db.execute("DELETE FROM messages WHERE saved_at < ?", (int(time.time()) - KEEP_DAYS * 86400,))
    db.commit()


# ---------- вспомогательное ----------

def esc(s: str | None) -> str:
    return html.escape(s or "")


def person(user) -> str:
    if not user:
        return "Неизвестный"
    name = " ".join(x for x in (user.first_name, user.last_name) if x)
    return f"{name} (@{user.username})" if user.username else name


def media_of(m: Message) -> tuple[str | None, str | None]:
    if m.photo:
        return "photo", m.photo[-1].file_id
    for kind in ("video", "voice", "video_note", "audio", "document", "animation", "sticker"):
        obj = getattr(m, kind)
        if obj:
            return kind, obj.file_id
    return None, None


MEDIA_NAMES = {
    "photo": "📷 Фото", "video": "🎬 Видео", "voice": "🎤 Голосовое", "video_note": "⭕️ Кружок",
    "audio": "🎵 Аудио", "document": "📎 Файл", "animation": "🎞 GIF", "sticker": "🩷 Стикер",
}


async def owner_of(bot: Bot, conn_id: str) -> int | None:
    row = db.execute("SELECT owner_id, enabled FROM connections WHERE id=?", (conn_id,)).fetchone()
    if row:
        return row[0] if row[1] else None
    try:  # бот перезапускался и не знает подключение — спрашиваем у Telegram
        conn = await bot.get_business_connection(conn_id)
        save_connection(conn)
        return conn.user_chat_id if conn.is_enabled else None
    except Exception as e:
        logging.error(f"Не удалось получить подключение {conn_id}: {e}")
        return None


def save_connection(conn: BusinessConnection):
    db.execute("INSERT OR REPLACE INTO connections VALUES (?, ?, ?)",
               (conn.id, conn.user_chat_id, int(conn.is_enabled)))
    db.commit()


def save_message(m: Message):
    media_type, file_id = media_of(m)
    chat_name = person(m.chat) if m.chat.type == "private" else (m.chat.title or "")
    db.execute("INSERT OR REPLACE INTO messages VALUES (?,?,?,?,?,?,?,?,?,?)", (
        m.business_connection_id, m.chat.id, m.message_id,
        m.from_user.id if m.from_user else 0, person(m.from_user), chat_name,
        m.text or m.caption or "", media_type, file_id, int(time.time()),
    ))
    db.commit()


async def send_media(bot: Bot, owner: int, media_type: str, file_id: str, caption: str):
    """Пересылает сохранённое медиа владельцу. У кружков и стикеров нет подписи — шлём её отдельно."""
    if media_type in ("video_note", "sticker"):
        await bot.send_message(owner, caption, parse_mode="HTML")
        caption = None
    send = {
        "photo": bot.send_photo, "video": bot.send_video, "voice": bot.send_voice,
        "video_note": bot.send_video_note, "audio": bot.send_audio, "document": bot.send_document,
        "animation": bot.send_animation, "sticker": bot.send_sticker,
    }[media_type]
    kwargs = {"caption": caption, "parse_mode": "HTML"} if caption else {}
    await send(owner, file_id, **kwargs)


# ---------- обработчики ----------

@router.business_connection()
async def on_connection(conn: BusinessConnection, bot: Bot):
    save_connection(conn)
    text = ("✅ <b>Бот подключён к твоим чатам.</b>\nБуду присылать удалённые и изменённые сообщения "
            f"собеседников. Копии хранятся {KEEP_DAYS} дней.\n\n"
            "📥 <b>Одноразовые медиа:</b> не открывая фото/видео/голосовое, ответь на него "
            "любым текстом — пришлю копию. Ответь словом «сии» — твой ответ сразу удалится, "
            "собеседник ничего не заметит."
            if conn.is_enabled else "⛔️ Бот отключён от твоих чатов.")
    try:
        await bot.send_message(conn.user_chat_id, text, parse_mode="HTML")
    except Exception as e:
        logging.error(f"Не удалось уведомить о подключении: {e}")


CATCH_TYPES = ("photo", "video", "voice", "video_note")
SILENT_WORD = "сии"


@router.business_message()
async def on_message(m: Message, bot: Bot):
    save_message(m)
    reply = m.reply_to_message
    if not reply:
        return
    owner = await owner_of(bot, m.business_connection_id)
    if not owner or not m.from_user or m.from_user.id != owner:
        return
    if reply.from_user and reply.from_user.id == owner:
        return  # ответ на своё же медиа
    await catch_media(bot, owner, m, reply)


async def catch_media(bot: Bot, owner: int, m: Message, reply: Message):
    """Ты ответил на медиа собеседника — присылаем копию (в т.ч. одноразовые фото и видео)."""
    media_type, file_id = media_of(reply)
    if media_type not in CATCH_TYPES or not file_id:
        return
    silent = (m.text or "").strip().lower() == SILENT_WORD
    if silent:
        # Удаляем твой ответ, чтобы собеседник ничего не заметил
        try:
            await bot.delete_business_messages(m.business_connection_id, [m.message_id])
        except Exception as e:
            logging.error(f"Не удалось удалить ответ: {e}")
    if db.execute("SELECT 1 FROM caught WHERE chat_id=? AND msg_id=?",
                  (reply.chat.id, reply.message_id)).fetchone():
        return  # это медиа уже присылали
    db.execute("INSERT INTO caught VALUES (?, ?)", (reply.chat.id, reply.message_id))
    db.commit()
    caption = f"📥 <b>Сохранил медиа</b> от {esc(person(reply.from_user))}"
    if reply.caption:
        caption += f"\n\n<blockquote>{esc(reply.caption)}</blockquote>"
    try:
        await send_media(bot, owner, media_type, file_id, caption[:1024])
    except Exception as e:
        logging.error(f"Не удалось переслать медиа: {e}")
        await bot.send_message(owner, "⚠️ Не получилось сохранить это медиа — Telegram не отдал файл боту.")


@router.edited_business_message()
async def on_edit(m: Message, bot: Bot):
    owner = await owner_of(bot, m.business_connection_id)
    old = db.execute("SELECT text FROM messages WHERE chat_id=? AND msg_id=?",
                     (m.chat.id, m.message_id)).fetchone()
    save_message(m)
    if not owner or (m.from_user and m.from_user.id == owner):
        return  # свои правки не присылаем
    new_text = m.text or m.caption or ""
    if old is None or old[0] == new_text:
        return
    await bot.send_message(owner, (
        f"✏️ <b>{esc(person(m.from_user))}</b> изменил(а) сообщение\n\n"
        f"<b>Было:</b>\n<blockquote>{esc(old[0]) or '—'}</blockquote>\n"
        f"<b>Стало:</b>\n<blockquote>{esc(new_text) or '—'}</blockquote>"
    ), parse_mode="HTML")


@router.deleted_business_messages()
async def on_delete(event: BusinessMessagesDeleted, bot: Bot):
    owner = await owner_of(bot, event.business_connection_id)
    if not owner:
        return
    for msg_id in event.message_ids:
        row = db.execute(
            "SELECT from_id, from_name, chat_name, text, media_type, file_id FROM messages "
            "WHERE chat_id=? AND msg_id=?", (event.chat.id, msg_id)).fetchone()
        if not row:
            continue  # сообщение пришло до подключения бота — его нет в базе
        from_id, from_name, chat_name, text, media_type, file_id = row
        if from_id == owner:
            continue  # свои удаления не присылаем
        head = f"🗑 <b>{esc(from_name)}</b> удалил(а) сообщение"
        if chat_name and chat_name != from_name:
            head += f" в чате «{esc(chat_name)}»"
        try:
            if media_type and file_id:
                caption = head + (f"\n\n<blockquote>{esc(text)}</blockquote>" if text else "")
                await send_media(bot, owner, media_type, file_id, caption[:1024])
            else:
                await bot.send_message(owner, f"{head}\n\n<blockquote>{esc(text) or '—'}</blockquote>",
                                       parse_mode="HTML")
        except Exception as e:
            logging.error(f"Не удалось переслать удалённое сообщение: {e}")
        db.execute("DELETE FROM messages WHERE chat_id=? AND msg_id=?", (event.chat.id, msg_id))
    db.commit()
