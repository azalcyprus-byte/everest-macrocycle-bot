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
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

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

scheduled_notification_keys: set[str] = set()
sent_notification_keys: set[str] = set()

BUTTON_LABELS = {
    "view": "Посмотреть",
    "approve": "Утвердить",
    "revise": "На доработку",
    "done": "Выполнено",
    "move": "Перенести",
}

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


def read_current_working_data() -> dict:
    """Read concrete current data from the working spreadsheet."""
    spreadsheet = get_spreadsheet()

    panel_sheet = spreadsheet.worksheet("00_Панель")
    panel_rows = panel_sheet.get(
        "A4:B16",
        value_render_option="FORMATTED_VALUE",
    )
    panel = {
        row[0]: row[1] if len(row) > 1 else ""
        for row in panel_rows
        if row and row[0]
    }

    day_sheet = spreadsheet.worksheet("02_День")
    day_rows = day_sheet.get(
        "A4:N88",
        value_render_option="FORMATTED_VALUE",
    )

    today_text = datetime.now(MOSCOW_TZ).strftime("%d.%m.%Y")
    today_row = next(
        (row for row in day_rows if row and row[0] == today_text),
        None,
    )

    if today_row is None:
        raise LookupError(
            f"Today row {today_text} was not found in 02_День."
        )

    padded = list(today_row) + [""] * (14 - len(today_row))

    return {
        "spreadsheet_title": spreadsheet.title,
        "panel": panel,
        "day": {
            "date": padded[0],
            "day_of_week": padded[1],
            "cycle_day": padded[2],
            "week": padded[3],
            "mesocycle": padded[4],
            "phase": padded[5],
            "day_type_plan": padded[6],
            "day_type_fact": padded[7],
            "total_load": padded[8],
            "key_task": padded[9],
            "game_plan": padded[10],
            "game_fact": padded[11],
            "study_plan": padded[12],
            "study_fact": padded[13],
        },
    }


def display_value(value: str) -> str:
    value = str(value).strip()
    return value if value else "—"


def test_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Посмотреть",
                    callback_data="button_test:view",
                ),
                InlineKeyboardButton(
                    "Утвердить",
                    callback_data="button_test:approve",
                ),
            ],
            [
                InlineKeyboardButton(
                    "На доработку",
                    callback_data="button_test:revise",
                ),
                InlineKeyboardButton(
                    "Выполнено",
                    callback_data="button_test:done",
                ),
            ],
            [
                InlineKeyboardButton(
                    "Перенести",
                    callback_data="button_test:move",
                ),
            ],
        ]
    )


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




async def readtest(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not is_authorized(update):
        logger.warning(
            "Unauthorized /readtest attempt from user_id=%s",
            getattr(update.effective_user, "id", None),
        )
        return

    await update.effective_message.reply_text(
        "Читаю рабочие данные из таблицы…"
    )

    try:
        data = await asyncio.to_thread(read_current_working_data)
        panel = data["panel"]
        day = data["day"]

        text = (
            "Чтение рабочих данных успешно ✅\n\n"
            f"Таблица: {data['spreadsheet_title']}\n"
            "Источники: 00_Панель и 02_День\n\n"
            "📍 Текущий контекст\n"
            f"Дата: {display_value(day['date'])}\n"
            f"День недели: {display_value(day['day_of_week'])}\n"
            f"День макроцикла: {display_value(day['cycle_day'])}\n"
            f"Неделя: {display_value(day['week'])}\n"
            f"Мезоцикл: {display_value(day['mesocycle'])}\n"
            f"Фаза: {display_value(day['phase'])}\n\n"
            "📋 План и факт дня\n"
            f"Day Type Plan: {display_value(day['day_type_plan'])}\n"
            f"Day Type Fact: {display_value(day['day_type_fact'])}\n"
            f"Ключевая задача: {display_value(day['key_task'])}\n"
            f"Игра: план {display_value(day['game_plan'])} ч | "
            f"факт {display_value(day['game_fact'])} ч\n"
            f"Study: план {display_value(day['study_plan'])} ч | "
            f"факт {display_value(day['study_fact'])} ч\n"
            f"Общая фактическая нагрузка: "
            f"{display_value(day['total_load'])} ч\n\n"
            "📊 Панель\n"
            f"Факт покера за макроцикл: "
            f"{display_value(panel.get('Факт покера, ч', ''))} ч\n"
            f"Цель: {display_value(panel.get('Цель, ч', ''))} ч\n"
            f"Среднее за календарный день: "
            f"{display_value(panel.get('Среднее/календарный день', ''))} ч\n"
            f"Средний сон за 7 дней: "
            f"{display_value(panel.get('Средний сон 7 дней', ''))} ч\n"
            f"Overheat за 7 дней: "
            f"{display_value(panel.get('Overheat 7 дней', ''))}"
        )

        await update.effective_message.reply_text(text)
    except Exception as exc:
        logger.exception("Working data read failed")
        await update.effective_message.reply_text(
            "Не удалось прочитать рабочие данные ❌\n"
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
            "/notifytest 12:15"
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
            f"Уведомление на {requested_time} уже запланировано — "
            "дубль заблокирован ✅"
        )
        return

    if key in sent_notification_keys:
        await update.effective_message.reply_text(
            f"Уведомление на {requested_time} уже было отправлено — "
            "повтор заблокирован ✅"
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
        f"Тестовое уведомление запланировано на {requested_time} "
        "по Москве ✅\n"
        "Повторная постановка на то же время будет заблокирована."
    )


async def buttonstest(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not is_authorized(update):
        logger.warning(
            "Unauthorized /buttonstest attempt from user_id=%s",
            getattr(update.effective_user, "id", None),
        )
        return

    await update.effective_message.reply_text(
        "🧪 Тестовая карточка управления\n\n"
        "Это только техническая проверка кнопок.\n"
        "Нажми любую кнопку — бот должен зафиксировать выбор.",
        reply_markup=test_keyboard(),
    )


async def handle_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    if query is None:
        return

    if query.from_user.id != ALLOWED_USER_ID:
        logger.warning(
            "Unauthorized button attempt from user_id=%s",
            query.from_user.id,
        )
        await query.answer("Доступ запрещён.", show_alert=True)
        return

    await query.answer()

    data = query.data or ""
    prefix = "button_test:"
    if not data.startswith(prefix):
        return

    action = data.removeprefix(prefix)
    label = BUTTON_LABELS.get(action, "Неизвестное действие")

    logger.info(
        "Test button pressed: action=%s user_id=%s",
        action,
        query.from_user.id,
    )

    original_text = (
        "🧪 Тестовая карточка управления\n\n"
        "Это только техническая проверка кнопок.\n"
        "Нажми любую кнопку — бот должен зафиксировать выбор."
    )
    await query.edit_message_text(
        text=(
            f"{original_text}\n\n"
            f"Последнее нажатие: «{label}» ✅"
        ),
        reply_markup=test_keyboard(),
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
    telegram_app.add_handler(CommandHandler("readtest", readtest))
    telegram_app.add_handler(CommandHandler("notifytest", notifytest))
    telegram_app.add_handler(CommandHandler("buttonstest", buttonstest))
    telegram_app.add_handler(
        CallbackQueryHandler(
            handle_button,
            pattern=r"^button_test:",
        )
    )
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
