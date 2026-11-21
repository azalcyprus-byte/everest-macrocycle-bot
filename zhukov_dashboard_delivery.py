"""Persistent one-message-per-date delivery for the cycle-2 PNG dashboard."""
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from zoneinfo import ZoneInfo

import gspread
from telegram import InputMediaPhoto
from telegram.error import BadRequest

import zhukov_dashboard as dashboard
import zhukov_evening_v2
import zhukov_monitor as monitor
import zhukov_plan
import zhukov_sleep

logger = logging.getLogger(__name__)
TAB = "Everest_Алексей_утро_v2"
HEADERS = ["Дата", "Chat ID", "Message ID", "SHA256", "Статус", "Обновлено UTC", "Ошибка"]
_lock = asyncio.Lock()
_last_inputs_state = None


def read_inputs(morning_book, evening_book, *, plans=None):
    """No old postgame source is opened anywhere in this path."""
    warnings = []
    sheet_tz = ZoneInfo(os.getenv("ZHUKOV_SHEET_TIMEZONE", "Asia/Shanghai"))
    morning_rows = morning_book.worksheet(
        os.getenv("ZHUKOV_MORNING_SHEET", "Утро — ответы")
    ).get_all_values()
    morning_headers = morning_rows[0] if morning_rows else []
    if not {"Дата", "Отметка времени"}.issubset(str(h).strip() for h in morning_headers):
        raise ValueError("Morning source schema mismatch")
    mornings = monitor.normalize_records(morning_rows, sheet_tz)
    # New dashboards require an explicit report date, even when legacy readers
    # can recover a date from the submission stamp.
    mornings = [r for r in mornings if monitor.parse_date(dashboard.field(r, "Дата")) == r["date"]]
    try:
        if evening_book is None:
            raise RuntimeError("New evening spreadsheet unavailable")
        evening_rows = evening_book.worksheet(
            os.getenv("ZHUKOV_EVENING_RESPONSES_TAB", "Form Responses 1")
        ).get_all_values()
        # This form's locale is en_US: slash dates are month/day/year.
        headers = [str(h).strip() for h in evening_rows[0]] if evening_rows else []
        if "Дата" in headers:
            column = headers.index("Дата")
            for row in evening_rows[1:]:
                if len(row) > column and "/" in str(row[column]):
                    try:
                        row[column] = datetime.strptime(row[column], "%m/%d/%Y").date().isoformat()
                    except ValueError:
                        row[column] = ""  # Never reinterpret an invalid US date as EU.
        evenings = zhukov_evening_v2.parse_records(evening_rows)
    except Exception:
        logger.exception("Morning dashboard: new evening input unavailable")
        evenings = []
        warnings.append("Новая вечерняя таблица недоступна; факт нагрузки не оценён.")
    try:
        bedtimes = dashboard.parse_bedtimes(morning_book.worksheet(zhukov_sleep.TAB).get_all_values())
    except gspread.exceptions.WorksheetNotFound:
        bedtimes = []
    except Exception:
        logger.exception("Morning dashboard: bedtime registry unavailable")
        bedtimes = []
        warnings.append("Реестр сна недоступен; расчётное время сна не оценено.")
    if plans is None:
        try:
            plans = zhukov_plan.load_daily_plan()
        except Exception:
            logger.exception("Morning dashboard: cycle-2 plan unavailable")
            plans = {}
            warnings.append("Таблица плана недоступна; план не задан.")
    return mornings, evenings, bedtimes, plans, warnings


def registry(book):
    try:
        sheet = book.worksheet(TAB)
    except gspread.exceptions.WorksheetNotFound:
        sheet = book.add_worksheet(title=TAB, rows=1000, cols=len(HEADERS))
        sheet.append_row(HEADERS)
    rows = sheet.get_all_values()
    if not rows or rows[0] != HEADERS:
        raise ValueError("Morning dashboard registry schema mismatch")
    return sheet, rows


def fingerprint(report):
    payload = json.dumps(report, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def upsert(bot, book, sheet, rows, report, *, now):
    """Reserve before send; corrections edit the saved Telegram message ID.

    A send timeout without a message ID stays REVIEW for manual reconciliation.
    An edit can be retried safely because it addresses that exact same message.
    """
    _, _, chat_id = monitor.config()
    key, digest = report["date"].isoformat(), fingerprint(report)
    matches = [(i + 1, row) for i, row in enumerate(rows) if row and row[0] == key]
    if len(matches) > 1:
        raise ValueError("Duplicate report registry date; sending paused")
    existing = matches[0] if matches else None
    if existing:
        row_number, old = existing
        old = old + [""] * (len(HEADERS) - len(old))
        if old[1] != str(chat_id):
            raise ValueError("Curator chat changed; refusing to duplicate a report")
        if old[3] == digest and old[4] == "SENT":
            return "unchanged"
        if not old[2]:
            # An interrupted send may already have reached Telegram.
            return "review"
        message_id = int(old[2])
    else:
        message_id = None
    photo = await asyncio.to_thread(dashboard.render_png, report)
    row = [key, str(chat_id), str(message_id or ""), digest, "SENDING", now.isoformat(), ""]
    if existing:
        await asyncio.to_thread(sheet.update, range_name=f"A{row_number}:G{row_number}", values=[row])
    else:
        await asyncio.to_thread(sheet.append_row, row)
        rows.append(row)
        row_number = len(rows)
    caption = f"Everest · Алексей Жуков · {report['date']:%d.%m.%Y}\nУтренний отчёт · Макроцикл 2"
    try:
        if message_id:
            try:
                await bot.edit_message_media(
                    chat_id=chat_id, message_id=message_id,
                    media=InputMediaPhoto(media=photo, caption=caption),
                )
            except BadRequest as exc:
                if "message is not modified" not in str(exc).casefold():
                    raise
        else:
            sent = await bot.send_photo(chat_id=chat_id, photo=photo, caption=caption)
            message_id = sent.message_id
            # Persist the ID immediately, before any optional audit operations.
        row[2], row[4] = str(message_id), "SENT"
    except Exception as exc:
        logger.exception("Morning dashboard delivery requires review for %s", key)
        row[4], row[6] = "REVIEW", f"{type(exc).__name__}: {exc}"[:400]
    await asyncio.to_thread(sheet.update, range_name=f"A{row_number}:G{row_number}", values=[row])
    rows[row_number - 1] = row
    if row[4] == "SENT":
        logger.info("Morning dashboard %s: %s message %s", key, "updated" if existing else "sent", message_id)
    return row[4].lower()


async def tick(bot, morning_book, evening_book, *, now=None, plans=None, target_day=None):
    if not monitor.enabled():
        return
    async with _lock:
        now = now or datetime.now(timezone.utc)
        mornings, evenings, bedtimes, plans, warnings = await asyncio.to_thread(
            read_inputs, morning_book, evening_book, plans=plans,
        )
        global _last_inputs_state
        state = (len(mornings), len(evenings), len(bedtimes), len(plans), tuple(warnings))
        if state != _last_inputs_state:
            logger.info("Morning dashboard inputs: mornings=%d evenings=%d bedtimes=%d cycle2_plan=%d warnings=%s",
                        *state[:4], state[4])
            _last_inputs_state = state
        eligible = []
        for record in mornings:
            day = record["date"]
            if not zhukov_plan.CYCLE_START <= day <= zhukov_plan.CYCLE_END:
                continue
            if target_day is not None and day != target_day:
                continue
            zone = dashboard.location_zone(record, plans.get(day))
            local_today = now.astimezone(zone or ZoneInfo(os.getenv("ZHUKOV_TIMEZONE", "Asia/Phnom_Penh"))).date()
            if day > local_today or record["timestamp"] is None or record["timestamp"] > now:
                continue
            eligible.append(record)
        sheet, rows = await asyncio.to_thread(registry, morning_book)
        activation = next((r for r in rows[1:] if r and r[0] == "ACTIVATION"), None)
        if activation is None:
            activation = ["ACTIVATION", "", "", "", "CONFIG", now.isoformat(), ""]
            await asyncio.to_thread(sheet.append_row, activation)
            rows.append(activation)
        activated = datetime.fromisoformat(activation[5])
        known = {r[0] for r in rows[1:] if r}
        results = []
        for record in eligible:
            key = record["date"].isoformat()
            # No automatic historical flood on first deployment. Correcting an
            # already-delivered date always updates that date's same message.
            if target_day is None and key not in known and record["timestamp"] < activated:
                zone = dashboard.location_zone(record, plans.get(record["date"]))
                today = now.astimezone(zone or ZoneInfo(os.getenv("ZHUKOV_TIMEZONE", "Asia/Phnom_Penh"))).date()
                if record["date"] != today:
                    continue
            report = dashboard.build_report(record, mornings, evenings, bedtimes, plans, warnings)
            results.append(await upsert(bot, morning_book, sheet, rows, report, now=now))
        return results
