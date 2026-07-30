import asyncio
import logging
import os

from aiohttp import web
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
TELEGRAM_USER_ID_RAW = os.environ.get("TELEGRAM_USER_ID")
PORT = int(os.environ.get("PORT", "8000"))

if not BOT_TOKEN:
    raise RuntimeError("Environment variable BOT_TOKEN is missing.")

if not TELEGRAM_USER_ID_RAW:
    raise RuntimeError("Environment variable TELEGRAM_USER_ID is missing.")

try:
    ALLOWED_USER_ID = int(TELEGRAM_USER_ID_RAW)
except ValueError as exc:
    raise RuntimeError("TELEGRAM_USER_ID must be an integer.") from exc


def is_authorized(update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id == ALLOWED_USER_ID)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        logger.warning("Unauthorized /start attempt from user_id=%s",
                       getattr(update.effective_user, "id", None))
        return

    await update.effective_message.reply_text(
        "Everest Macrocycle Bot запущен ✅\n"
        "Авторизация подтверждена."
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        logger.warning("Unauthorized message from user_id=%s",
                       getattr(update.effective_user, "id", None))
        return

    text = update.effective_message.text or ""
    await update.effective_message.reply_text(
        f"Сообщение принято координатором:\n\n{text}"
    )


async def health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def run_health_server() -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info("Health server started on port %s", PORT)
    return runner


async def main() -> None:
    telegram_app = Application.builder().token(BOT_TOKEN).build()

    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )

    health_runner = await run_health_server()

    try:
        await telegram_app.initialize()
        await telegram_app.start()
        await telegram_app.updater.start_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )
        logger.info("Telegram bot started.")
        await asyncio.Event().wait()
    finally:
        await telegram_app.updater.stop()
        await telegram_app.stop()
        await telegram_app.shutdown()
        await health_runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
