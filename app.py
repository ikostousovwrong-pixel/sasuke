import os
import random
import asyncio
import logging
from datetime import datetime

import aiosqlite
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
from functools import wraps

# ================= Настройка =================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)

load_dotenv()
TG_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # https://your-app.onrender.com/<TOKEN>
PORT = int(os.getenv("PORT", 8000))
CHANNEL_USERNAME = "@fanbotpage"

if not TG_TOKEN or not OPENAI_API_KEY or not WEBHOOK_URL:
    raise RuntimeError("Не хватает переменных окружения TELEGRAM_TOKEN / OPENAI_API_KEY / WEBHOOK_URL")

client = OpenAI(api_key=OPENAI_API_KEY)
TOS_VERSION = 1
MAX_TURNS = 8
LONG_PROB = 0.5
SYSTEM_PROMPT = "Ты пародийная версия актёра Джейкоба Элорди. ... (сокращено для примера)"

# ================= Декоратор обработки ошибок =================
def safe_update(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            await func(update, context)
        except Exception as e:
            logging.error(f"Ошибка в {func.__name__}: {e}", exc_info=True)
            if update.message:
                await update.message.reply_text("Произошла ошибка, попробуйте позже.")
    return wrapper

# ================= База данных =================
DB_PATH = "consent.db"

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS tos_acceptance (
            user_id       INTEGER PRIMARY KEY,
            accepted_at   TEXT NOT NULL,
            version       INTEGER NOT NULL,
            age_confirmed INTEGER NOT NULL
        )
        """)
        await db.commit()

async def has_accepted(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        row = await db.execute_fetchone("SELECT version FROM tos_acceptance WHERE user_id = ?", (user_id,))
        return row is not None and int(row[0]) == TOS_VERSION

async def set_accepted(user_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        INSERT INTO tos_acceptance (user_id, accepted_at, version, age_confirmed)
        VALUES (?, ?, ?, 1)
        ON CONFLICT(user_id) DO UPDATE SET
            accepted_at = excluded.accepted_at,
            version = excluded.version,
            age_confirmed = excluded.age_confirmed
        """, (user_id, datetime.utcnow().isoformat(), TOS_VERSION))
        await db.commit()

async def delete_acceptance(user_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM tos_acceptance WHERE user_id = ?", (user_id,))
        await db.commit()

# ================= Онбординг =================
def consent_text() -> str:
    return (
        "Добро пожаловать! Для продолжения подтвердите, что вам есть 18+ "
        "и вы согласны с условиями пользования и политикой конфиденциальности.\n"
        "Не забудьте проверить подписку на наш канал https://t.me/fanbotpage"
    )

def consent_kb() -> InlineKeyboardMarkup:
    TERMS_URL = "https://telegra.ph/YOUR_TERMS"
    PRIVACY_URL = "https://telegra.ph/YOUR_PRIVACY"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Подтверждаю", callback_data="consent_accept")],
        [InlineKeyboardButton("Отклоняю", callback_data="consent_decline")],
        [InlineKeyboardButton("Условия", url=TERMS_URL),
         InlineKeyboardButton("Политика", url=PRIVACY_URL)]
    ])

@safe_update
async def send_consent_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(consent_text(), reply_markup=consent_kb())

# ================= Проверка подписки =================
async def is_subscribed(bot, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(CHANNEL_USERNAME, user_id)
        return member.status in ["member", "administrator", "creator"]
    except Exception as e:
        logging.warning(f"Ошибка проверки подписки для {user_id}: {e}")
        return False

# ================= Обработчики =================
@safe_update
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await is_subscribed(context.bot, user_id):
        await update.message.reply_text(
            f"Подпишитесь на наш канал, чтобы пользоваться ботом: https://t.me/fanbotpage\n"
            "После подписки нажмите /start ещё раз."
        )
        return
    if not await has_accepted(user_id):
        await send_consent_message(update, context)
        return

    context.user_data.setdefault("history", [])
    await update.message.reply_text(
        "Хей! Это пародийный фанбот. Истории, советы или просто разговор по душам.\n"
        "Команды: /help, /reset"
    )

@safe_update
async def on_consent_accept(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if not await is_subscribed(context.bot, user_id):
        await query.message.reply_text("Сначала подпишитесь на канал: https://t.me/fanbotpage")
        return

    await set_accepted(user_id)
    await query.edit_message_text("Спасибо! Доступ открыт. Можете отправить сообщение или /start.")

@safe_update
async def on_consent_decline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await delete_acceptance(query.from_user.id)
    await query.edit_message_text("Вы отклонили условия. Чтобы вернуться, используйте /start.")

@safe_update
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Примеры запросов:\n"
        "— как дела?\n"
        "— придумай свидание / давай романтики\n"
        "— сделай комплимент\n"
        "— совет по стилю\n"
        "— расскажи про дела\n"
        "Команда /reset — очистить контекст диалога."
    )

@safe_update
async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["history"] = []
    await update.message.reply_text("Контекст очищен. С чего начнём заново?")

# ================= LLM =================
def build_messages(history: list[dict], user_text: str, mode: str) -> list[dict]:
    length_rule = "Отвечай максимально кратко (3-5 слов)." if mode == "short" else \
                  "Дай развернутый ответ около 180–220 токенов."
    sys_prompt = SYSTEM_PROMPT + "\nПравило длины: " + length_rule
    msgs = [{"role": "system", "content": sys_prompt}]
    msgs += history[-2*MAX_TURNS:]  # безопасная длина истории
    msgs.append({"role": "user", "content": user_text})
    return msgs

def llm_reply(messages: list[dict], mode: str) -> str:
    for _ in range(3):  # retry
        try:
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                temperature=0.8 if mode == "long" else 0.5,
                max_tokens=220 if mode == "long" else 35,
                messages=messages
            )
            return resp.choices[0].message.content.strip()
        except Exception as e:
            logging.warning(f"LLM ошибка: {e}, retry...")
    return "Занят. Напиши позже."

@safe_update
async def talk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not await is_subscribed(context.bot, user_id):
        await update.message.reply_text(f"Подпишитесь на канал: https://t.me/fanbotpage")
        return
    if not await has_accepted(user_id):
        await send_consent_message(update, context)
        return

    text = (update.message.text or "").strip()
    if not text:
        return

    context.user_data.setdefault("history", [])
    history = context.user_data["history"]

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
async def main():
    await init_db()
    app = Application.builder().token(TG_TOKEN).build()

    # Онбординг
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(on_consent_accept, pattern="^consent_accept$"))
    app.add_handler(CallbackQueryHandler(on_consent_decline, pattern="^consent_decline$"))

    # Команды
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("reset", reset_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, talk))

    # Устанавливаем webhook
    await app.bot.set_webhook(WEBHOOK_URL)
    logging.info(f"Webhook установлен на {WEBHOOK_URL}")

    # Запуск сервера Render
    from hypercorn.asyncio import serve
    from hypercorn.config import Config
    config = Config()
    config.bind = [f"0.0.0.0:{PORT}"]
    await serve(app, config)

if __name__ == "__main__":
    asyncio.run(main())

