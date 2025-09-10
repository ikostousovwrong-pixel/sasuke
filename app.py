import os, json
from fastapi import FastAPI, Request, HTTPException
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters

app = FastAPI()
BOTS = {}  # username(lower) -> {"app": Application, "secret": str}

async def cmd_start(update, context):
    await update.message.reply_text("Привет! Бот работает.")

async def echo(update, context):
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

@app.on_event("startup")
async def on_startup():
    base_url = os.environ["BASE_URL"].rstrip("/")
    for username, data in BOTS.items():
        await data["app"].bot.set_webhook(
            url=f"{base_url}/webhook/{username}",
            secret_token=data["secret"]
        )

@app.post("/webhook/{username}")
async def webhook(username: str, request: Request):
    username = username.lower()
    if username not in BOTS:
        raise HTTPException(status_code=404, detail="unknown bot")
    secret_header = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if secret_header != BOTS[username]["secret"]:
        raise HTTPException(status_code=403, detail="bad secret")
    data = await request.json()
    application = BOTS[username]["app"]
    update = Update.de_json(data, application.bot)
    await application.process_update(update)
    return {"ok": True}
