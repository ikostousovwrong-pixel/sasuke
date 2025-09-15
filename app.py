import os
import random
import sqlite3
import logging
from datetime import datetime
import asyncio

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
from aiohttp import web

# ========== Логирование ==========
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s | %(message)s")

# ========== ENV ==========
load_dotenv()
TG_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # например: https://multi-telegram-bots.onrender.com
SYSTEM_PROMPT_FILE = os.getenv("SYSTEM_PROMPT_FILE")

if not all([TG_TOKEN, OPENAI_API_KEY, WEBHOOK_URL, SYSTEM_PROMPT_FILE]):
    raise RuntimeError("Не хватает переменных окружения!")

with open(SYSTEM_PROMPT_FILE, encoding="utf-8") as f:
    SYSTEM_PROMPT = f.read().strip()

client = OpenAI(api_key=OPENAI_API_KEY)

CHANNEL_USERNAME = "@fanbotpage"
TOS_VERSION = 1
MAX_TURNS = 8
LONG_PROB = 0.5

# ========== БД ==========
conn = sqlite3.connect("consent.db", check_same_thread=False)
conn.execute("""
CREATE TABLE IF NOT EXISTS tos_acceptance (
    user_id INTEGER PRIMARY KEY,
    accepted_at TEXT NOT NULL,
    version INTEGER NOT NULL,
    age_confirmed INTEGER NOT NULL
)
""")
conn.commit()

def has_accepted(user_id: int) -> bool:
    row = conn.execute("SELECT version FROM tos_acceptance WHERE user_id=?", (user_id,)).fetchone()
    return row is not None and int(row[0]) == TOS_VERSION

def set_accepted(user_id: int):
    conn.execute("""
    INSERT INTO tos_acceptance (user_id, accepted_at, version, age_confirmed)
    VALUES (?, ?, ?, 1)
    ON CONFLICT(user_id) DO UPDATE SET
        accepted_at=excluded.accepted_at,
        version=excluded.version,
        age_confirmed=excluded.age_confirmed
    """, (user_id, datetime.utcnow().isoformat(), TOS_VERSION))
    conn.commit()

def delete_acceptance(user_id: int):
    conn.execute("DELETE FROM tos_acceptance WHERE user_id=?", (user_id,))
    conn.commit()

# ========== Consent ==========
def consent_text() -> str:
    return f"Подтвердите 18+ и согласие с правилами. Канал: https://t.me/{CHANNEL_USERNAME.strip('@')}"

def consent_kb() -> InlineKeyboardMarkup:
    TERMS_URL = "https://telegra.ph/YOUR_TERMS"
    PRIVACY_URL = "https://telegra.ph/YOUR_PRIVACY"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Подтверждаю", callback_data="consent_accept")],
        [InlineKeyboardButton("Отклоняю", callback_data="consent_decline")],
        [InlineKeyboardButton("Условия", url=TERMS_URL),
         InlineKeyboardButton("Политика", url=PRIVACY_URL)]
    ])

async def send_consent_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(consent_text(), reply_markup=consent_kb())

async def is_subscribed(bot, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(CHANNEL_USERNAME, user_id)
        return member.status in ["member", "administrator", "creator"]
    except:
        return False

# ========== Handlers ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await is_subscribed(context.bot, user_id):
        await update.message.reply_text(f"Подпишитесь на канал: https://t.me/{CHANNEL_USERNAME.strip('@')}")
        return
    if not has_accepted(user_id):
        await send_consent_message(update, context)
        return
    context.user_data.setdefault("history", [])
    await update.message.reply_text("Привет! /help для команд.")

async def consent_accept(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    set_accepted(user_id)
    await query.edit_message_text("Спасибо! Доступ открыт.")

async def consent_decline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    delete_acceptance(query.from_user.id)
    await query.edit_message_text("Вы отклонили условия.")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("/reset — очистить историю.")

async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["history"] = []
    await update.message.reply_text("История очищена.")

# ========== LLM ==========
def build_messages(history, user_text, mode):
    length_rule = "Кратко." if mode=="short" else "Развернуто ~200 токенов."
    msgs = [{"role": "system", "content": SYSTEM_PROMPT + "\n" + length_rule}]
    msgs += history[-2*MAX_TURNS:]
    msgs.append({"role": "user", "content": user_text})
    return msgs

def llm_reply(messages, mode):
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0.8 if mode=="long" else 0.5,
            max_tokens=220 if mode=="long" else 55,
            messages=messages
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        logging.error(e)
        return "Ошибка LLM"

async def talk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await is_subscribed(context.bot, user_id) or not has_accepted(user_id):
        await send_consent_message(update, context)
        return
    text = update.message.text.strip()
    if not text:
        return
    history = context.user_data.setdefault("history", [])
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    mode = "long" if random.random() < LONG_PROB else "short"
    messages = build_messages(history, text, mode)
    reply = await asyncio.to_thread(llm_reply, messages, mode)
    await update.message.reply_text(reply)
    history.append({"role":"user","content":text})
    history.append({"role":"assistant","content":reply})
    context.user_data["history"] = history[-2*MAX_TURNS:]

# ========== Main ==========
def main():
    app = Application.builder().token(TG_TOKEN).build()

    # Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("reset", reset_cmd))
    app.add_handler(CallbackQueryHandler(consent_accept, pattern="^consent_accept$"))
    app.add_handler(CallbackQueryHandler(consent_decline, pattern="^consent_decline$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, talk))

    PORT = int(os.environ.get("PORT", 8000))
    WEBHOOK_PATH = "/webhook"
    WEBHOOK_FULL_URL = f"{WEBHOOK_URL}{WEBHOOK_PATH}"

    async def handle(request):
        data = await request.json()
        update = Update.de_json(data, app.bot)
        await app.initialize()
        await app.process_update(update)
        return web.Response(text="ok")

    async def on_startup(_):
        await app.initialize()
        await app.bot.set_webhook(WEBHOOK_FULL_URL)
        logging.info(f"Webhook установлен: {WEBHOOK_FULL_URL}")

    web_app = web.Application()
    web_app.router.add_post(WEBHOOK_PATH, handle)
    web_app.on_startup.append(on_startup)

    logging.info(f"Сервер запускается на 0.0.0.0:{PORT}")
    web.run_app(web_app, host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    main()
