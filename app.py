import os
import json
import asyncio
import logging
from typing import Any
from fastapi import FastAPI, Request, HTTPException
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()
BOTS: dict[str, dict[str, Any]] = {}

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text("Привет! Бот работает.")

async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message and update.message.text:
        await update.message.reply_text(update.message.text)

def make_application(token: str) -> Application:
    application = Application.builder().token(token).build()
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))
    return application

bots_config = json.loads(os.environ.get("BOTS_JSON", "[]"))

for cfg in bots_config:
    username = cfg["username"].lower()
    token = cfg["token"]
    secret = cfg["secret"]
    app_instance = make_application(token)
    BOTS[username] = {"app": app_instance, "secret": secret}

@app.get("/")
async def root():
    return {"ok": True, "bots": list(BOTS.keys())}

@app.on_event("startup")
async def on_startup():
    base_url = os.environ.get("BASE_URL", "").rstrip("/")
    if not base_url:
        raise RuntimeError("BASE_URL env var is not set")
    drop_pending = os.environ.get("DROP_PENDING_UPDATES", "false").lower() in ("1", "true", "yes")
    for username, data in BOTS.items():
        application: Application = data["app"]
        await application.initialize()
        await application.start()
        await application.bot.set_webhook(
            url=f"{base_url}/webhook/{username}",
            secret_token=data["secret"],
            drop_pending_updates=drop_pending,
        )
        logger.info("Webhook set for %s", username)

@app.on_event("shutdown")
async def on_shutdown():
    for username, data in BOTS.items():
        application: Application = data["app"]
        try:
            await application.bot.delete_webhook()
        except Exception:
            pass
        try:
            await application.stop()
        finally:
            await application.shutdown()

@app.post("/webhook/{username}")
async def webhook(username: str, request: Request):
    username = username.lower()
    entry = BOTS.get(username)
    if not entry:
        raise HTTPException(status_code=404, detail="unknown bot")
    secret_header = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if secret_header != entry["secret"]:
        raise HTTPException(status_code=403, detail="bad secret")
    data = await request.json()
    application: Application = entry["app"]
    if not getattr(application, "running", False):
        try:
            await application.initialize()
        except Exception:
            pass
        try:
            await application.start()
        except Exception:
            pass
    update = Update.de_json(data, application.bot)
    asyncio.create_task(application.process_update(update))
    return {"ok": True}
