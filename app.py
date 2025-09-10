import os
import json
from fastapi import FastAPI, Request, HTTPException
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

app = FastAPI()
BOTS: dict[str, dict] = {}  # username(lower) -> {"app": Application, "secret": str}

# Handlers
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
if update.message:
    await update.message.reply_text("Привет! Бот работает.")

async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
if update.message and update.message.text:
    await update.message.reply_text(update.message.text)

# Factory
def make_application(token: str) -> Application:
application = Application.builder().token(token).build()
application.add_handler(CommandHandler("start", cmd_start))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))
return application

# Load config from env
# BOTS_JSON пример:
# [
#   {"username":"Jacob_Elordi_Love_Bot","token":"8413551192:AAGLcTBXKJi7JM9qeukASP4LLzV3-pdlCb4","secret":"jacob_2s9Q-7nFg_1X"},
# ]
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
# Инициализируем и запускаем каждое приложение, затем ставим вебхуки
for username, data in BOTS.items():
    application: Application = data["app"]
    await application.initialize()
    await application.start()
    await application.bot.set_webhook(
        url=f"{base_url}/webhook/{username}",
        secret_token=data["secret"],
        drop_pending_updates=True,  # сбросить старые апдейты при первом запуске
        allowed_updates=["message", "edited_message", "callback_query", "chat_member", "my_chat_member", "inline_query"]
    )

@app.on_event("shutdown")
async def on_shutdown():
# Корректно снимаем вебхуки и останавливаем приложения
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
if username not in BOTS:
    raise HTTPException(status_code=404, detail="unknown bot")

# Проверка секрета из заголовка
secret_header = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
if secret_header != BOTS[username]["secret"]:
    raise HTTPException(status_code=403, detail="bad secret")

data = await request.json()
application: Application = BOTS[username]["app"]
update = Update.de_json(data, application.bot)
await application.process_update(update)
return {"ok": True}
