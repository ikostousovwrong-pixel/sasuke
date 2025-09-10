import os
import random
import sqlite3
import asyncio
import logging
from datetime import datetime

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from openai import OpenAI

# === Настройки и инициализация ===
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)

load_dotenv()
TG_TOKEN = os.getenv("TELEGRAM_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # https://yourapp.onrender.com/<token>
if not TG_TOKEN or not WEBHOOK_URL:
    raise RuntimeError("Не найден TELEGRAM_TOKEN или WEBHOOK_URL в .env")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    raise RuntimeError("Не найден OPENAI_API_KEY в .env")

client = OpenAI(api_key=OPENAI_API_KEY)
CHANNEL_USERNAME = "@fanbotpage"

# === SQLite база согласий ===
DB_PATH = os.path.join(os.path.dirname(__file__), "consent.db")
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.execute("""
CREATE TABLE IF NOT EXISTS tos_acceptance (
    user_id INTEGER PRIMARY KEY,
    accepted_at TEXT NOT NULL,
    version INTEGER NOT NULL,
    age_confirmed INTEGER NOT NULL
)
""")
conn.commit()

TOS_VERSION = 1

def has_accepted(user_id: int) -> bool:
    row = conn.execute("SELECT version FROM tos_acceptance WHERE user_id=?", (user_id,)).fetchone()
    return row is not None and int(row[0]) == TOS_VERSION

def set_accepted(user_id: int) -> None:
    conn.execute("""
    INSERT INTO tos_acceptance (user_id, accepted_at, version, age_confirmed)
    VALUES (?, ?, ?, 1)
    ON CONFLICT(user_id) DO UPDATE SET
        accepted_at = excluded.accepted_at,
        version = excluded.version,
        age_confirmed = excluded.age_confirmed
    """, (user_id, datetime.utcnow().isoformat(), TOS_VERSION))
    conn.commit()

def delete_acceptance(user_id: int) -> None:
    conn.execute("DELETE FROM tos_acceptance WHERE user_id=?", (user_id,))
    conn.commit()

# === Онбординг / подписка ===
async def is_subscribed(bot, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(CHANNEL_USERNAME, user_id)
        return member.status in ["member", "administrator", "creator"]
    except Exception:
        return False

def consent_text() -> str:
    return ("Добро пожаловать! Подтвердите, что вам есть 18 и вы согласны с условиями. "
            "Не забудьте подписаться на канал https://t.me/fanbotpage")

def consent_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Подтверждаю", callback_data="consent_accept")],
        [InlineKeyboardButton("Отклоняю", callback_data="consent_decline")],
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not has_accepted(user_id):
        await update.message.reply_text(consent_text(), reply_markup=consent_kb())
        return
    if not await is_subscribed(context.bot, user_id):
        await update.message.reply_text(
            "Подпишитесь на наш канал: https://t.me/fanbotpage и повторите /start"
        )
        return
    context.user_data.setdefault("history", [])
    await update.message.reply_text(
        "Привет! Это пародийный фанбот. Пиши сообщения для диалога.\n"
        "Команды: /help, /reset"
    )

async def on_consent_accept(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    user_id = q.from_user.id
    if not await is_subscribed(context.bot, user_id):
        await q.answer()
        await q.message.reply_text("Подпишитесь на канал и нажмите /start")
        return
    set_accepted(user_id)
    await q.answer("Согласие принято")
    await q.edit_message_text("Спасибо! Доступ открыт.")

async def on_consent_decline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    delete_acceptance(q.from_user.id)
    await q.answer()
    await q.edit_message_text("Вы отказались от условий. Доступ закрыт.")

# === Основные команды ===
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Примеры:\n— как дела?\n— придумай свидание\n— совет по стилю\n"
        "Команда /reset — очистить контекст."
    )

async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["history"] = []
    await update.message.reply_text("Контекст очищен.")

# === Токен / Webhook ===
def main():
    app = Application.builder().token(TG_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("reset", reset_cmd))
    app.add_handler(CallbackQueryHandler(on_consent_accept, pattern="^consent_accept$"))
    app.add_handler(CallbackQueryHandler(on_consent_decline, pattern="^consent_decline$"))

    # Запуск Webhook для Render
    import asyncio
    async def run():
        await app.bot.set_webhook(WEBHOOK_URL)
        print("Webhook установлен:", WEBHOOK_URL)
        await app.start()
        await app.updater.start_polling()  # только для совместимости, можно убрать
        await asyncio.Event().wait()  # держим процесс активным

    asyncio.run(run())

if __name__ == "__main__":
    main()
