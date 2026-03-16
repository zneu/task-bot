import asyncio
import logging
import os

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters
from bot.handlers import handle_message, handle_voice, handle_slash_command, handle_callback
from database.connection import init_db
from bot.scheduler import start_scheduler

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

app = FastAPI()
telegram_app = None


@app.get("/health")
async def health():
    return {"status": "ok"}


async def start_telegram():
    global telegram_app
    telegram_app = (
        Application.builder()
        .token(os.getenv("TELEGRAM_BOT_TOKEN"))
        .build()
    )
    # Slash commands
    for cmd in ["help", "list", "tasks", "done", "doing", "add", "edit", "delete", "del", "people", "dump"]:
        telegram_app.add_handler(CommandHandler(cmd, handle_slash_command))

    telegram_app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )
    telegram_app.add_handler(
        MessageHandler(filters.VOICE, handle_voice)
    )
    telegram_app.add_handler(CallbackQueryHandler(handle_callback))

    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.updater.start_polling()
    logger.info("Telegram bot started polling")


@app.on_event("startup")
async def startup():
    await init_db()
    logger.info("Database tables created")
    await start_telegram()
    start_scheduler(telegram_app)
    logger.info("Bot ready")


@app.on_event("shutdown")
async def shutdown():
    if telegram_app:
        await telegram_app.updater.stop()
        await telegram_app.stop()
        await telegram_app.shutdown()
