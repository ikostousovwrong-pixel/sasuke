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
from aiohttp import web

# ================= Настройка логирования =================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)

# ================= Загрузка переменных окружения =================
load_dotenv()
TG_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
SYSTEM_PROMPT_FILE = os.getenv("SYSTEM_PROMPT_FILE")

if not TG_TOKEN or not OPENAI_API_KEY or not WEBHOOK_URL or not SYSTEM_PROMPT_FILE:
    raise RuntimeError(
        "Не хватает переменных окружения TELEGRAM_TOKEN / OPENAI_API_KEY / WEBHOOK_URL / SYSTEM_PROMPT_FILE"
    )

# Читаем системный промпт из файла
with open(SYSTEM_PROMPT_FILE, "r", encoding="utf-8") as f:
    SYSTEM_PROMPT = f.read().strip()

client = OpenAI(api_key=OPENAI_API_KEY)

CHANNEL_USERNAME = "@fanbotpage"
TOS_VERSION = 1
MAX_TURNS = 8
LONG_PROB = 0.5

# ================= База данных =================
conn = sqlite3.connect("consent.db", check_same_thread=False)
conn.execute("""
CREATE TABLE IF NOT EXISTS tos_acceptance (
    user_id       INTEGER PRIMARY KEY,
    accepted_at   TEXT NOT NULL,
    version       INTEGER NOT NULL,
    age_confirmed INTEGER NOT NULL
)
""")
conn.commit()

def has_accepted(user_id: int) -> bool:
    row = conn.execute("SELECT version FROM tos_acceptance WHERE user_id = ?", (user_id,)).fetchone()
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
    conn.execute("DELETE FROM tos_acceptance WHERE user_id = ?", (user_id,))
    conn.commit()

# ================= Онбординг =================
def consent_text() -> str:
    return (
        "Добро пожаловать! Для продолжения подтвердите, что вам есть 18+ "
        "и вы согласны с условиями пользования и политикой конфиденциальности.\n"
        f"Не забудьте проверить подписку на наш канал https://t.me/{CHANNEL_USERNAME.strip('@')}"
    )

def consent_kb() -> InlineKeyboardMarkup:
    TERMS_URL = "https://telegra.ph/YOUR_TERMS"
    PRIVACY_URL = "https://telegra.ph/YOUR_PRIVACY"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Подтверждаю", callback_data="consent_accept")],
        [InlineKeyboardButton("Отклоняю", callback_data="consent_decline")],
        [
            InlineKeyboardButton("Условия", url=TERMS_URL),
            InlineKeyboardButton("Политика", url=PRIVACY_URL)
        ]
    ])

async def send_consent_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(consent_text(), reply_markup=consent_kb())

async def is_subscribed(bot, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(CHANNEL_USERNAME, user_id)
        return member.status in ["member", "administrator", "creator"]
    except Exception:
        return False

# ================= Обработчики =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await is_subscribed(context.bot, user_id):
        await update.message.reply_text(
            f"Подпишитесь на наш канал: https://t.me/{CHANNEL_USERNAME.strip('@')}\nПосле подписки нажмите /start."
        )
        return
    if not has_accepted(user_id):
        await send_consent_message(update, context)
        return

    context.user_data.setdefault("history", [])
    await update.message.reply_text(
        "Привет! Это общий шаблон бота.\nКоманды: /help, /reset"
    )

async def on_consent_accept(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if not await is_subscribed(context.bot, user_id):
        await query.message.reply_text(f"Сначала подпишитесь на канал: https://t.me/{CHANNEL_USERNAME.strip('@')}")
        return

    set_accepted(user_id)
    await query.edit_message_text("Спасибо! Доступ открыт. Можете отправить сообщение или /start.")

async def on_consent_decline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    delete_acceptance(query.from_user.id)
    await query.edit_message_text("Вы отклонили условия. Чтобы вернуться, используйте /start.")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Примеры запросов:\n— как дела?\n— придумай свидание\n— совет по стилю\nКоманда /reset — очистить контекст диалога."
    )

async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["history"] = []
    await update.message.reply_text("Контекст очищен. С чего начнём заново?")

# ================= LLM =================
def build_messages(history: list[dict], user_text: str, mode: str) -> list[dict]:
    length_rule = "Отвечай максимально кратко (3-5 слов)." if mode == "short" else \
                  "Дай развернутый ответ около 180–220 токенов."
    msgs = [{"role": "system", "content": SYSTEM_PROMPT + "\n" + length_rule}]
    msgs += history[-2*MAX_TURNS:]
    msgs.append({"role": "user", "content": user_text})
    return msgs

def llm_reply(messages: list[dict], mode: str) -> str:
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0.8 if mode == "long" else 0.5,
            max_tokens=220 if mode == "long" else 35,
            messages=messages
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        logging.error(f"LLM error: {e}")
        return "Занят. Напиши позже."

async def talk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await is_subscribed(context.bot, user_id):
        await update.message.reply_text(f"Подпишитесь на канал: https://t.me/{CHANNEL_USERNAME.strip('@')}")
        return
    if not has_accepted(user_id):
        await send_consent_message(update, context)
        return

    text = (update.message.text or "").strip()
    if not text:
        return

    history = context.user_data.setdefault("history", [])
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    text_l = text.lower()
    mode = "long" if any(k in text_l for k in ("#long", "подробнее")) else \
           "short" if any(k in text_l for k in ("#short", "кратко")) else \
           "long" if random.random() < LONG_PROB else "short"

    messages = build_messages(history, text, mode)
    reply = await asyncio.to_thread(llm_reply, messages, mode)

    await update.message.reply_text(reply)
    history.append({"role": "user", "content": text})
    history.append({"role": "assistant", "content": reply})
    context.user_data["history"] = history[-2*MAX_TURNS:]

# ================= Main =================
def main():
    app = Application.builder().token(TG_TOKEN).build()

    # Онбординг и команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(on_consent_accept, pattern="^consent_accept$"))
    app.add_handler(CallbackQueryHandler(on_consent_decline, pattern="^consent_decline$"))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("reset", reset_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, talk))

    PORT = int(os.environ.get("PORT", 8000))
    WEBHOOK_PATH = f"/{TG_TOKEN}"
    WEBHOOK_FULL_URL = f"{WEBHOOK_URL}/{TG_TOKEN}"

    async def handle(request):
        data = await request.json()
        update = Update.de_json(data, app.bot)
        await app.process_update(update)
        return web.Response(text="ok")

    async def on_startup(web_app):
        await app.bot.set_webhook(WEBHOOK_FULL_URL)
        logging.info(f"Webhook установлен на: {WEBHOOK_FULL_URL}")

    web_app = web.Application()
    web_app.router.add_post(WEBHOOK_PATH, handle)
    web_app.on_startup.append(on_startup)

    logging.info(f"Сервер запускается на 0.0.0.0:{PORT}")
    web.run_app(web_app, host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    main()
