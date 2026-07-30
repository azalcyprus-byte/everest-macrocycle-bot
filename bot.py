import asyncio
import json
import logging
import os
import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import gspread
from aiohttp import web
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
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
GOOGLE_CALENDAR_ID = os.environ.get(
    "GOOGLE_CALENDAR_ID",
    "azalcyprus@gmail.com",
)
PORT = int(os.environ.get("PORT", "8000"))

MOSCOW_TZ = ZoneInfo("Europe/Moscow")
TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")

scheduled_notification_keys: set[str] = set()
sent_notification_keys: set[str] = set()
write_test_lock = asyncio.Lock()
event_test_lock = asyncio.Lock()

WRITE_TEST_SHEET = "10_План_факт_дня"
WRITE_TEST_CELL = "R504"
WRITE_TEST_ROW_RANGE = "A504:Z504"
FORMULA_COLUMN_INDEXES = {
    "B": 1,
    "C": 2,
    "I": 8,
    "L": 11,
    "M": 12,
    "N": 13,
    "S": 18,
    "Y": 24,
    "Z": 25,
}

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



def _single_cell_value(rows: list[list[str]]) -> str:
    if not rows or not rows[0]:
        return ""
    return str(rows[0][0])


def _padded_row(rows: list[list[str]], width: int = 26) -> list[str]:
    row = list(rows[0]) if rows else []
    return row + [""] * (width - len(row))


def _formula_snapshot(row: list[str]) -> dict[str, str]:
    return {
        column: str(row[index])
        for column, index in FORMULA_COLUMN_INDEXES.items()
    }


def run_plan_fact_write_test() -> dict:
    """Write, read back, clear and verify one reserved plan-fact cell."""
    spreadsheet = get_spreadsheet()
    worksheet = spreadsheet.worksheet(WRITE_TEST_SHEET)

    original_cell = _single_cell_value(
        worksheet.get(
            WRITE_TEST_CELL,
            value_render_option="FORMATTED_VALUE",
        )
    )
    if original_cell.strip():
        raise RuntimeError(
            f"Reserved test cell {WRITE_TEST_SHEET}!{WRITE_TEST_CELL} "
            "is not empty."
        )

    before_row = _padded_row(
        worksheet.get(
            WRITE_TEST_ROW_RANGE,
            value_render_option="FORMULA",
        )
    )
    formulas_before = _formula_snapshot(before_row)

    timestamp = datetime.now(MOSCOW_TZ).strftime("%Y%m%d-%H%M%S")
    marker = f"MC3-BOT-WRITE-TEST-{timestamp}"

    try:
        worksheet.update(
            range_name=WRITE_TEST_CELL,
            values=[[marker]],
            value_input_option="RAW",
        )

        written_value = _single_cell_value(
            worksheet.get(
                WRITE_TEST_CELL,
                value_render_option="FORMATTED_VALUE",
            )
        )
        if written_value != marker:
            raise RuntimeError("Read-back value does not match written value.")

        after_write_row = _padded_row(
            worksheet.get(
                WRITE_TEST_ROW_RANGE,
                value_render_option="FORMULA",
            )
        )
        formulas_after_write = _formula_snapshot(after_write_row)
        if formulas_after_write != formulas_before:
            raise RuntimeError("Formula integrity check failed after writing.")

    finally:
        worksheet.batch_clear([WRITE_TEST_CELL])

    cleared_value = _single_cell_value(
        worksheet.get(
            WRITE_TEST_CELL,
            value_render_option="FORMATTED_VALUE",
        )
    )
    if cleared_value.strip():
        raise RuntimeError("Reserved test cell was not cleared.")

    after_clear_row = _padded_row(
        worksheet.get(
            WRITE_TEST_ROW_RANGE,
            value_render_option="FORMULA",
        )
    )
    formulas_after_clear = _formula_snapshot(after_clear_row)
    if formulas_after_clear != formulas_before:
        raise RuntimeError("Formula integrity check failed after cleanup.")

    return {
        "spreadsheet_title": spreadsheet.title,
        "sheet": WRITE_TEST_SHEET,
        "cell": WRITE_TEST_CELL,
        "marker": marker,
        "formula_count": len(formulas_before),
    }




def get_calendar_service():
    if not GOOGLE_SERVICE_ACCOUNT_JSON:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON is missing.")

    service_account_info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    credentials = Credentials.from_service_account_info(
        service_account_info,
        scopes=["https://www.googleapis.com/auth/calendar"],
    )
    return build(
        "calendar",
        "v3",
        credentials=credentials,
        cache_discovery=False,
    )


def _parse_calendar_boundary(raw: dict, *, is_end: bool = False) -> datetime:
    date_time = raw.get("dateTime")
    if date_time:
        normalized = date_time.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=MOSCOW_TZ)
        return parsed.astimezone(MOSCOW_TZ)

    date_value = raw.get("date")
    if not date_value:
        raise ValueError("Calendar event boundary has no date or dateTime.")

    parsed_date = date.fromisoformat(date_value)
    # Для all-day событий Google передаёт конец как исключающую дату.
    return datetime.combine(parsed_date, time.min, tzinfo=MOSCOW_TZ)


def _event_interval(item: dict) -> dict:
    start_raw = item.get("start", {})
    end_raw = item.get("end", {})
    start = _parse_calendar_boundary(start_raw)
    end = _parse_calendar_boundary(end_raw, is_end=True)

    if end <= start:
        raise ValueError("Calendar event has an invalid time interval.")

    return {
        "id": item.get("id", ""),
        "title": item.get("summary") or "Без названия",
        "start": start,
        "end": end,
        "all_day": "date" in start_raw,
        "transparent": item.get("transparency") == "transparent",
        "status": item.get("status", "confirmed"),
    }


def _find_calendar_conflicts(events: list[dict]) -> list[tuple[dict, dict]]:
    blocking = [
        event
        for event in events
        if event["status"] != "cancelled" and not event["transparent"]
    ]
    blocking.sort(key=lambda event: (event["start"], event["end"]))

    conflicts: list[tuple[dict, dict]] = []
    for index, left in enumerate(blocking):
        for right in blocking[index + 1:]:
            if right["start"] >= left["end"]:
                break
            if left["start"] < right["end"] and right["start"] < left["end"]:
                conflicts.append((left, right))
    return conflicts


def _conflict_engine_self_test() -> bool:
    base = datetime(2026, 1, 1, 10, 0, tzinfo=MOSCOW_TZ)
    first = {
        "id": "self-1",
        "title": "A",
        "start": base,
        "end": base + timedelta(hours=2),
        "all_day": False,
        "transparent": False,
        "status": "confirmed",
    }
    second = {
        "id": "self-2",
        "title": "B",
        "start": base + timedelta(hours=1),
        "end": base + timedelta(hours=3),
        "all_day": False,
        "transparent": False,
        "status": "confirmed",
    }
    third = {
        "id": "self-3",
        "title": "C",
        "start": base + timedelta(hours=3),
        "end": base + timedelta(hours=4),
        "all_day": False,
        "transparent": False,
        "status": "confirmed",
    }
    return len(_find_calendar_conflicts([first, second, third])) == 1


def read_calendar_and_conflicts(days: int = 7) -> dict:
    if days < 1 or days > 30:
        raise ValueError("Calendar test period must be between 1 and 30 days.")

    service = get_calendar_service()
    calendar = service.calendars().get(
        calendarId=GOOGLE_CALENDAR_ID,
    ).execute()

    now = datetime.now(MOSCOW_TZ)
    period_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    period_end = period_start + timedelta(days=days)

    items: list[dict] = []
    page_token = None
    while True:
        response = service.events().list(
            calendarId=GOOGLE_CALENDAR_ID,
            timeMin=period_start.isoformat(),
            timeMax=period_end.isoformat(),
            singleEvents=True,
            orderBy="startTime",
            maxResults=250,
            pageToken=page_token,
        ).execute()
        items.extend(response.get("items", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break

    events: list[dict] = []
    invalid_events = 0
    for item in items:
        if item.get("status") == "cancelled":
            continue
        try:
            events.append(_event_interval(item))
        except (TypeError, ValueError):
            invalid_events += 1
            logger.exception(
                "Could not parse calendar event id=%s",
                item.get("id"),
            )

    conflicts = _find_calendar_conflicts(events)
    if not _conflict_engine_self_test():
        raise RuntimeError("Calendar conflict engine self-test failed.")

    return {
        "calendar_summary": calendar.get("summary") or GOOGLE_CALENDAR_ID,
        "calendar_id": GOOGLE_CALENDAR_ID,
        "calendar_timezone": calendar.get("timeZone") or "не указана",
        "period_start": period_start,
        "period_end": period_end,
        "events": events,
        "conflicts": conflicts,
        "invalid_events": invalid_events,
    }




def _calendar_marker_matches(
    service,
    marker: str,
    period_start: datetime,
    period_end: datetime,
) -> list[dict]:
    response = service.events().list(
        calendarId=GOOGLE_CALENDAR_ID,
        timeMin=period_start.isoformat(),
        timeMax=period_end.isoformat(),
        singleEvents=True,
        showDeleted=False,
        maxResults=20,
        privateExtendedProperty=f"mc3_test_marker={marker}",
    ).execute()
    return response.get("items", [])


def run_calendar_event_write_test() -> dict:
    """Create, read, update and delete one isolated calendar event."""
    service = get_calendar_service()
    calendar = service.calendars().get(
        calendarId=GOOGLE_CALENDAR_ID,
    ).execute()

    now = datetime.now(MOSCOW_TZ)
    original_start = (
        now.replace(second=0, microsecond=0) + timedelta(minutes=15)
    )
    original_end = original_start + timedelta(minutes=15)
    updated_start = original_start + timedelta(minutes=30)
    updated_end = updated_start + timedelta(minutes=20)

    marker = f"MC3-CAL-{now.strftime('%Y%m%d-%H%M%S-%f')}"
    original_title = "MC3 BOT TEST — будет удалено"
    updated_title = "MC3 BOT TEST — обновлено и будет удалено"
    search_start = original_start - timedelta(days=1)
    search_end = updated_end + timedelta(days=1)

    event_id = ""
    created_ok = False
    duplicate_check_ok = False
    updated_ok = False
    deleted_ok = False

    body = {
        "summary": original_title,
        "description": (
            "Технический тест блока №23. "
            "Событие создаётся, проверяется, изменяется и удаляется автоматически."
        ),
        "start": {
            "dateTime": original_start.isoformat(),
            "timeZone": "Europe/Moscow",
        },
        "end": {
            "dateTime": original_end.isoformat(),
            "timeZone": "Europe/Moscow",
        },
        # Тест не должен блокировать реальное расписание даже на несколько секунд.
        "transparency": "transparent",
        "reminders": {"useDefault": False},
        "extendedProperties": {
            "private": {
                "mc3_test_marker": marker,
                "mc3_purpose": "block_23_calendar_write_test",
            }
        },
    }

    try:
        created = service.events().insert(
            calendarId=GOOGLE_CALENDAR_ID,
            body=body,
            sendUpdates="none",
        ).execute()
        event_id = created.get("id", "")
        if not event_id:
            raise RuntimeError("Calendar API did not return an event ID.")

        read_created = service.events().get(
            calendarId=GOOGLE_CALENDAR_ID,
            eventId=event_id,
        ).execute()
        created_interval = _event_interval(read_created)
        created_marker = (
            read_created.get("extendedProperties", {})
            .get("private", {})
            .get("mc3_test_marker")
        )
        if (
            read_created.get("summary") != original_title
            or created_marker != marker
            or created_interval["start"] != original_start
            or created_interval["end"] != original_end
        ):
            raise RuntimeError("Created calendar event failed read-back check.")
        created_ok = True

        matches = _calendar_marker_matches(
            service,
            marker,
            search_start,
            search_end,
        )
        if len(matches) != 1 or matches[0].get("id") != event_id:
            raise RuntimeError(
                "Duplicate protection check failed after event creation."
            )
        duplicate_check_ok = True

        patch_body = {
            "summary": updated_title,
            "start": {
                "dateTime": updated_start.isoformat(),
                "timeZone": "Europe/Moscow",
            },
            "end": {
                "dateTime": updated_end.isoformat(),
                "timeZone": "Europe/Moscow",
            },
        }
        service.events().patch(
            calendarId=GOOGLE_CALENDAR_ID,
            eventId=event_id,
            body=patch_body,
            sendUpdates="none",
        ).execute()

        read_updated = service.events().get(
            calendarId=GOOGLE_CALENDAR_ID,
            eventId=event_id,
        ).execute()
        updated_interval = _event_interval(read_updated)
        preserved_marker = (
            read_updated.get("extendedProperties", {})
            .get("private", {})
            .get("mc3_test_marker")
        )
        if (
            read_updated.get("summary") != updated_title
            or preserved_marker != marker
            or updated_interval["start"] != updated_start
            or updated_interval["end"] != updated_end
        ):
            raise RuntimeError("Updated calendar event failed read-back check.")
        updated_ok = True

        service.events().delete(
            calendarId=GOOGLE_CALENDAR_ID,
            eventId=event_id,
            sendUpdates="none",
        ).execute()

        # После удаления Google Calendar может вернуть 404/410 либо
        # оставить объект доступным только как cancelled. Оба варианта
        # означают успешное удаление из активного календаря.
        try:
            deleted_event = service.events().get(
                calendarId=GOOGLE_CALENDAR_ID,
                eventId=event_id,
            ).execute()
        except HttpError as exc:
            if exc.resp.status not in (404, 410):
                raise
        else:
            if deleted_event.get("status") != "cancelled":
                raise RuntimeError(
                    "Deleted calendar event is still active."
                )

        remaining = _calendar_marker_matches(
            service,
            marker,
            search_start,
            search_end,
        )
        if remaining:
            raise RuntimeError("Test event or duplicate remained after cleanup.")
        deleted_ok = True
        event_id = ""

    finally:
        if event_id:
            try:
                service.events().delete(
                    calendarId=GOOGLE_CALENDAR_ID,
                    eventId=event_id,
                    sendUpdates="none",
                ).execute()
            except HttpError as exc:
                if exc.resp.status not in (404, 410):
                    logger.exception(
                        "Emergency cleanup of test calendar event failed"
                    )
            except Exception:
                logger.exception(
                    "Emergency cleanup of test calendar event failed"
                )

    if not all((created_ok, duplicate_check_ok, updated_ok, deleted_ok)):
        raise RuntimeError("Calendar write test did not complete all checks.")

    return {
        "calendar_summary": calendar.get("summary") or GOOGLE_CALENDAR_ID,
        "marker": marker,
        "original_start": original_start,
        "updated_start": updated_start,
        "created_ok": created_ok,
        "duplicate_check_ok": duplicate_check_ok,
        "updated_ok": updated_ok,
        "deleted_ok": deleted_ok,
    }


def _format_event_line(event: dict) -> str:
    if event["all_day"]:
        end_inclusive = event["end"] - timedelta(days=1)
        if event["start"].date() == end_inclusive.date():
            period = event["start"].strftime("%d.%m, весь день")
        else:
            period = (
                f"{event['start'].strftime('%d.%m')}–"
                f"{end_inclusive.strftime('%d.%m')}, весь день"
            )
    else:
        if event["start"].date() == event["end"].date():
            period = (
                f"{event['start'].strftime('%d.%m %H:%M')}–"
                f"{event['end'].strftime('%H:%M')}"
            )
        else:
            period = (
                f"{event['start'].strftime('%d.%m %H:%M')}–"
                f"{event['end'].strftime('%d.%m %H:%M')}"
            )
    return f"• {period} — {event['title']}"


def _format_calendar_report(data: dict) -> str:
    events = data["events"]
    conflicts = data["conflicts"]
    period_end_inclusive = data["period_end"] - timedelta(days=1)

    lines = [
        "Чтение Google Calendar работает ✅",
        "",
        f"Календарь: {data['calendar_summary']}",
        f"Период: {data['period_start'].strftime('%d.%m.%Y')}–"
        f"{period_end_inclusive.strftime('%d.%m.%Y')}",
        f"Событий: {len(events)}",
        f"Конфликтов: {len(conflicts)}",
        "Механизм поиска пересечений: PASS ✅",
    ]

    if data["invalid_events"]:
        lines.append(
            f"Не удалось разобрать событий: {data['invalid_events']}"
        )

    lines.extend(["", "📅 Ближайшие события"])
    if events:
        for event in events[:15]:
            lines.append(_format_event_line(event))
        if len(events) > 15:
            lines.append(f"…и ещё {len(events) - 15}")
    else:
        lines.append("• На выбранный период событий нет.")

    lines.extend(["", "⚠️ Пересечения"])
    if conflicts:
        for left, right in conflicts[:10]:
            lines.append(
                f"• «{left['title']}» пересекается с «{right['title']}»"
            )
        if len(conflicts) > 10:
            lines.append(f"…и ещё {len(conflicts) - 10}")
    else:
        lines.append("• Пересечений не найдено.")

    text = "\n".join(lines)
    if len(text) > 3900:
        text = text[:3850] + "\n…Ответ сокращён из-за лимита Telegram."
    return text


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




async def writetest(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not is_authorized(update):
        logger.warning(
            "Unauthorized /writetest attempt from user_id=%s",
            getattr(update.effective_user, "id", None),
        )
        return

    if write_test_lock.locked():
        await update.effective_message.reply_text(
            "Тест записи уже выполняется. Подожди несколько секунд."
        )
        return

    await update.effective_message.reply_text(
        "Проверяю безопасную запись в план-факт…"
    )

    async with write_test_lock:
        try:
            result = await asyncio.to_thread(run_plan_fact_write_test)
            await update.effective_message.reply_text(
                "Запись в план-факт работает ✅\n\n"
                f"Таблица: {result['spreadsheet_title']}\n"
                f"Вкладка: {result['sheet']}\n"
                f"Тестовая ячейка: {result['cell']}\n\n"
                "Проверки:\n"
                "• значение записано;\n"
                "• значение прочитано обратно;\n"
                f"• проверено формул: {result['formula_count']};\n"
                "• тестовая запись удалена;\n"
                "• формулы не изменились.\n\n"
                "Рабочие данные и показатели дня не изменены."
            )
        except Exception as exc:
            logger.exception("Plan-fact write test failed")
            await update.effective_message.reply_text(
                "Тест записи в план-факт не пройден ❌\n"
                f"Ошибка: {type(exc).__name__}"
            )




async def calendartest(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not is_authorized(update):
        logger.warning(
            "Unauthorized /calendartest attempt from user_id=%s",
            getattr(update.effective_user, "id", None),
        )
        return

    days = 7
    if context.args:
        if len(context.args) != 1 or not context.args[0].isdigit():
            await update.effective_message.reply_text(
                "Формат команды:\n/calendartest\n"
                "или /calendartest 14"
            )
            return
        days = int(context.args[0])
        if days < 1 or days > 30:
            await update.effective_message.reply_text(
                "Период должен быть от 1 до 30 дней."
            )
            return

    await update.effective_message.reply_text(
        "Читаю календарь и проверяю пересечения…"
    )

    try:
        data = await asyncio.to_thread(read_calendar_and_conflicts, days)
        await update.effective_message.reply_text(
            _format_calendar_report(data)
        )
    except Exception as exc:
        logger.exception("Calendar read test failed")
        await update.effective_message.reply_text(
            "Не удалось прочитать Google Calendar ❌\n"
            f"Ошибка: {type(exc).__name__}"
        )




async def eventtest(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not is_authorized(update):
        logger.warning(
            "Unauthorized /eventtest attempt from user_id=%s",
            getattr(update.effective_user, "id", None),
        )
        return

    if event_test_lock.locked():
        await update.effective_message.reply_text(
            "Тест календарного события уже выполняется. "
            "Подожди несколько секунд."
        )
        return

    await update.effective_message.reply_text(
        "Создаю изолированное тестовое событие, "
        "проверяю изменение и удаляю его…"
    )

    async with event_test_lock:
        try:
            result = await asyncio.to_thread(run_calendar_event_write_test)
            await update.effective_message.reply_text(
                "Создание и изменение событий работает ✅\n\n"
                f"Календарь: {result['calendar_summary']}\n"
                f"Первичное время: "
                f"{result['original_start'].strftime('%d.%m.%Y %H:%M')}\n"
                f"Изменённое время: "
                f"{result['updated_start'].strftime('%d.%m.%Y %H:%M')}\n\n"
                "Проверки:\n"
                "• событие создано и прочитано обратно;\n"
                "• обнаружен ровно один экземпляр;\n"
                "• название и время изменены;\n"
                "• изменения прочитаны обратно;\n"
                "• событие удалено из активного календаря;\n"
                "• тестовых дублей не осталось.\n\n"
                "Реальные события календаря не изменены."
            )
        except Exception as exc:
            logger.exception("Calendar event write test failed")
            await update.effective_message.reply_text(
                "Тест создания и изменения события не пройден ❌\n"
                f"Ошибка: {type(exc).__name__}\n"
                "Тестовое событие будет удалено аварийной очисткой, "
                "если оно успело создаться."
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
    telegram_app.add_handler(CommandHandler("writetest", writetest))
    telegram_app.add_handler(CommandHandler("calendartest", calendartest))
    telegram_app.add_handler(CommandHandler("eventtest", eventtest))
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
