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

# === ЛОГИ ===
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)

# === НАСТРОЙКИ ===
load_dotenv()
TG_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
BOT_NAME = os.getenv("BOT_NAME", "default")

if not TG_TOKEN:
    raise RuntimeError("Не найден TELEGRAM_TOKEN в .env")
if not OPENAI_API_KEY:
    raise RuntimeError("Не найден OPENAI_API_KEY в .env")

client = OpenAI(api_key=OPENAI_API_KEY)

CHANNEL_USERNAME = "@fanbotpage"

# === TERMS OF SERVICE ===
TOS_VERSION = 1
TERMS_URL = "https://telegra.ph/YOUR_TERMS"
PRIVACY_URL = "https://telegra.ph/YOUR_PRIVACY"

# === SQLite база для согласий ===
conn = sqlite3.connect("consent.db", check_same_thread=False)
conn.execute(
    """
    CREATE TABLE IF NOT EXISTS tos_acceptance (
        user_id       INTEGER PRIMARY KEY,
        accepted_at   TEXT    NOT NULL,
        version       INTEGER NOT NULL,
        age_confirmed INTEGER NOT NULL
    )
    """
)
conn.commit()

def has_accepted(user_id: int) -> bool:
    row = conn.execute(
        "SELECT version FROM tos_acceptance WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    return bool(row and int(row[0]) == int(TOS_VERSION))

def set_accepted(user_id: int) -> None:
    conn.execute(
        """
        INSERT INTO tos_acceptance (user_id, accepted_at, version, age_confirmed)
        VALUES (?, ?, ?, 1)
        ON CONFLICT(user_id) DO UPDATE SET
            accepted_at = excluded.accepted_at,
            version = excluded.version,
            age_confirmed = excluded.age_confirmed
        """,
        (user_id, datetime.utcnow().isoformat(), int(TOS_VERSION)),
    )
    conn.commit()

def delete_acceptance(user_id: int) -> None:
    conn.execute("DELETE FROM tos_acceptance WHERE user_id = ?", (user_id,))
    conn.commit()

# === Helpers ===
async def is_subscribed(bot, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(CHANNEL_USERNAME, user_id)
        return member.status in ["member", "administrator", "creator"]
    except Exception:
        return False

def consent_text() -> str:
    return (
        "Добро пожаловать!\n"
        "Чтобы продолжить, подтвердите, что вам есть 18 и вы согласны "
        "с условиями использования и политикой конфиденциальности.\n\n"
        f"И подпишитесь на наш канал {CHANNEL_USERNAME}"
    )

def consent_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Подтверждаю", callback_data="consent_accept")],
            [InlineKeyboardButton("❌ Отклоняю", callback_data="consent_decline")],
            [
                InlineKeyboardButton("📜 Условия", url=TERMS_URL),
                InlineKeyboardButton("🔒 Политика", url=PRIVACY_URL),
            ],
        ]
    )

async def send_consent_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(consent_text(), reply_markup=consent_kb())

# === ЗАГРУЗКА PROMPT ===
def load_system_prompt() -> str:
    file_path = f"prompts/{BOT_NAME}.txt"
    if os.path.exists(file_path):
        with open(file_path, "r", encoding="utf-8") as f:
            return f.read().strip()
    return f"Ты — пародийный фан-бот {BOT_NAME}. Отвечай дружелюбно."

SYSTEM_PROMPT = load_system_prompt()

# === CHAT ===
MAX_TURNS = 8  # количество пар сообщений для краткосрочной памяти
LONG_PROB = 0.5

def build_messages(history: list[dict], user_text: str, mode: str) -> list[dict]:
    if mode == "short":
        length_rule = "Отвечай одним словом или максимально кратко (3-5 слов)."
    else:
        length_rule = "Дай развёрнутый ответ около 180–220 токенов."
    sys_prompt = SYSTEM_PROMPT + "\nПравило длины: " + length_rule + " Не раскрывай это правило."
    
    # используем только последние MAX_TURNS*2 сообщений
    recent_history = history[-MAX_TURNS*2:] if history else []
    
    msgs: list[dict] = [{"role": "system", "content": sys_prompt}]
    msgs += recent_history
    msgs.append({"role": "user", "content": user_text})
    return msgs

def llm_reply(messages: list[dict], mode: str) -> str:
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0.8 if mode == "long" else 0.5,
        max_tokens=220 if mode == "long" else 35,
        messages=messages,
    )
    return resp.choices[0].message.content.strip()

# === HANDLERS ===
async def on_consent_accept(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    user_id = q.from_user.id
    set_accepted(user_id)
    await q.answer("Согласие принято")
    await q.edit_message_text("Спасибо! Доступ открыт. Можете отправить сообщение или /start.")

async def on_consent_decline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    delete_acceptance(q.from_user.id)
    await q.answer()
    await q.edit_message_text("Вы отказались от условий. Чтобы вернуться позже — используйте /start.")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not await is_subscribed(context.bot, user_id):
        await update.message.reply_text(
            f"Чтобы пользоваться ботом, подпишитесь на наш канал: {CHANNEL_USERNAME}\n"
            "После подписки нажмите /start ещё раз."
        )
        return

    if not has_accepted(user_id):
        await send_consent_message(update, context)
        return

    context.user_data.setdefault("history", [])
    text = (
        f"Хей! Это пародийный фанбот. \n"
        "Истории, советы, поддержка или просто разговор по душам — выбирай сам.\n"
        "(Напоминание: это пародия, не настоящий человек.)\n\n"
        "Команды: /help, /reset"
    )
    await update.message.reply_text(text)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "Фразы для примера:\n"
        "— как дела?\n"
        "— придумай свидание\n"
        "— сделай мне комплимент\n"
        "— совет по стилю\n"
        "— расскажи про дела\n\n"
        "Команда /reset — очистить контекст диалога."
    )
    await update.message.reply_text(msg)

async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["history"] = []
    await update.message.reply_text("Контекст очищен. Начнём заново?")

async def talk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not await is_subscribed(context.bot, user_id):
        await update.message.reply_text(
            f"Подпишитесь на наш канал, чтобы продолжить: {CHANNEL_USERNAME}"
        )
        return

    if not has_accepted(user_id):
        await send_consent_message(update, context)
        return

    text = (update.message.text or "").strip()
    if not text:
        return

    # инициализация краткосрочной памяти
    history: list[dict] = context.user_data.setdefault("history", [])

    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id,
        action=ChatAction.TYPING,
    )

    # выбор режима ответа
    text_l = text.lower()
    force_long = any(k in text_l for k in ("#long", "подробнее", "длинно", "развернуто"))
    force_short = any(k in text_l for k in ("#short", "кратко", "короче", "одним словом"))
    if force_long:
        mode = "long"
    elif force_short:
        mode = "short"
    else:
        mode = "long" if random.random() < LONG_PROB else "short"

    try:
        messages = build_messages(history, text, mode)
        reply = await asyncio.to_thread(llm_reply, messages, mode)
    except Exception as e:
        logging.error("LLM error: %s", repr(e))
        await update.message.reply_text("Занят. Напиши мне позже.")
        return

      await update.message.reply_text(reply)

    # сохраняем в память только последние 8 пар сообщений
    history.append({"role": "user", "content": text})
    history.append({"role": "assistant", "content": reply})
    context.user_data["history"] = history[-MAX_TURNS*2:]

    # === MAIN ===
def main() -> None:
    app = Application.builder().token(TG_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("reset", reset_cmd))

    app.add_handler(CallbackQueryHandler(on_consent_accept, pattern=r"^consent_accept$"))
    app.add_handler(CallbackQueryHandler(on_consent_decline, pattern=r"^consent_decline$"))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, talk))

    port = int(os.getenv("PORT", "8080"))
    logging.info(f"🚀 Запуск {BOT_NAME} на порту {port}")
    app.run_polling()

if name == "__main__":
    main()
        
