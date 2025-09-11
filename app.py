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
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # пример: https://multi-telegram-bots.onrender.com
SYSTEM_PROMPT_FILE = os.getenv("SYSTEM_PROMPT_FILE")

# ===== Проверка переменных окружения =====
required_vars = {
    "TELEGRAM_TOKEN": TG_TOKEN,
    "OPENAI_API_KEY": OPENAI_API_KEY,
    "WEBHOOK_URL": WEBHOOK_URL,
    "SYSTEM_PROMPT_FILE": SYSTEM_PROMPT_FILE,
}

missing_vars = [name for name, value in required_vars.items() if not value]
if missing_vars:
    raise RuntimeError(f"Не хватает переменных окружения: {', '.join(missing_vars)}")

logging.info("Переменные окружения загружены:")
for name, value in required_vars.items():
    display = value if name
