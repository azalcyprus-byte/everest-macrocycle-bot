import asyncio
import json
import logging
import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import gspread
from aiohttp import web
from google.oauth2.service_account import Credentials
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
TELEGRAM_USER_ID_RAW = os.environ.get("TELEGRAM_USER_ID")
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID")
PORT = int(os.environ.get("PORT", "8000"))

MOSCOW_TZ = ZoneInfo("Europe/Moscow")
TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")

# Временная защита от дублей в рамках текущего запуска процесса.
# Постоянный журнал будет добавлен на отдельном этапе дорожной карты.
scheduled_notification_keys: set[str] = set()
sent_notification_keys: set[str] = set()

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


def get_spreadsheet():
    if not GOOGLE_SERVICE_ACCOUNT_JSON:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON is missing.")
    if not GOOGLE_SHEET_ID:
        raise RuntimeError("GOOGLE_SHEET_ID is missing.")

    service_account_info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    credentials = Credentials.from_service_account_info(
        service_account_info,
        scopes=scopes,
    )
    client = gspread.authorize(credentials)
    return client.open_by_key(GOOGLE_SHEET_ID)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        logger.warning(
            "Unauthorized /start attempt from user_id=%s",
            getattr(update.effective_user, "id", None),
        )
        return

    await update.effective_message.reply_text(
        "Everest Macrocycle Bot запущен ✅\n"
        "Авторизация подтверждена."
    )


async def sheet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        logger.warning(
            "Unauthorized /sheet attempt from user_id=%s",
            getattr(update.effective_user, "id", None),
        )
        return

    try:
        spreadsheet = await asyncio.to_thread(get_spreadsheet)
        worksheets = await asyncio.to_thread(spreadsheet.worksheets)
        sheet_names = [ws.title for ws in worksheets]

        preview = "\n".join(f"• {name}" for name in sheet_names[:10])
        extra = ""
        if len(sheet_names) > 10:
            extra = f"\n…и ещё {len(sheet_names) - 10}"

        await update.effective_message.reply_text(
            "Связь с Google Sheets установлена ✅\n\n"
            f"Таблица: {spreadsheet.title}\n"
            f"Листов: {len(sheet_names)}\n\n"
            f"{preview}{extra}"
        )
    except Exception as exc:
        logger.exception("Google Sheets connection failed")
        await update.effective_message.reply_text(
            "Не удалось подключиться к Google Sheets ❌\n"
            f"Ошибка: {type(exc).__name__}"
        )


async def deliver_test_notification(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = context.job.data
    key = data["key"]
    chat_id = data["chat_id"]
    planned_time = data["planned_time"]

    if key in sent_notification_keys:
        logger.warning("Duplicate notification blocked: %s", key)
        return

    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            "⏰ Тестовое плановое уведомление\n\n"
            f"Запланированное время: {planned_time} по Москве.\n"
            "Доставка подтверждена ✅"
        ),
    )
    sent_notification_keys.add(key)
    scheduled_notification_keys.discard(key)
    logger.info("Test notification delivered: %s", key)


async def notifytest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        logger.warning(
            "Unauthorized /notifytest attempt from user_id=%s",
            getattr(update.effective_user, "id", None),
        )
        return

    if len(context.args) != 1 or not TIME_PATTERN.fullmatch(context.args[0]):
        await update.effective_message.reply_text(
            "Укажи время по Москве в формате:\n"
            "/notifytest 11:50"
        )
        return

    requested_time = context.args[0]
    hour, minute = map(int, requested_time.split(":"))

    now = datetime.now(MOSCOW_TZ)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

    if target <= now:
        await update.effective_message.reply_text(
            f"Время {requested_time} по Москве уже прошло.\n"
            "Назначь время на несколько минут вперёд."
        )
        return

    chat_id = update.effective_chat.id
    key = f"{chat_id}:{target.isoformat()}"

    if key in scheduled_notification_keys:
        await update.effective_message.reply_text(
            f"Уведомление на {requested_time} уже запланировано — дубль заблокирован ✅"
        )
        return

    if key in sent_notification_keys:
        await update.effective_message.reply_text(
            f"Уведомление на {requested_time} уже было отправлено — повтор заблокирован ✅"
        )
        return

    context.job_queue.run_once(
        deliver_test_notification,
        when=target,
        data={
            "key": key,
            "chat_id": chat_id,
            "planned_time": requested_time,
        },
        name=key,
        chat_id=chat_id,
    )
    scheduled_notification_keys.add(key)

    await update.effective_message.reply_text(
        f"Тестовое уведомление запланировано на {requested_time} по Москве ✅\n"
        "Повторная постановка на то же время будет заблокирована."
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        logger.warning(
            "Unauthorized message from user_id=%s",
            getattr(update.effective_user, "id", None),
        )
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
    telegram_app.add_handler(CommandHandler("sheet", sheet))
    telegram_app.add_handler(CommandHandler("notifytest", notifytest))
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
