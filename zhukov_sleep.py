"""Private Telegram 'going to bed' markers for Alexey's own Google Sheet.

A marker records when Telegram received the button press. It is NOT a
measurement of sleep onset or sleep duration. Keep the UTC instant so
historical observations survive travel and timezone changes.
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import gspread


TAB = "Everest_Алексей_сон"
HEADERS = (
    "EVENT_KEY", "EVENT_TYPE", "LOCAL_DATE", "LOCAL_TIME",
    "UTC_TIMESTAMP", "TIMEZONE", "SOURCE",
)
EVENT_TYPE = "BEDTIME_BUTTON"
DUPLICATE_WINDOW = timedelta(minutes=30)


def _aware_utc(clicked_at):
    """Accept Telegram's UTC message time, reject ambiguous naive instants."""
    if not isinstance(clicked_at, datetime) or clicked_at.tzinfo is None:
        raise ValueError("Timezone-aware Telegram timestamp required")
    return clicked_at.astimezone(timezone.utc)


def save_bedtime(spreadsheet, clicked_at, *, local_timezone="Asia/Phnom_Penh"):
    """Persist a bedtime intent exactly once within a 30-minute window.

    Returns (created: bool, local_timestamp: datetime). Only Alexey's own
    spreadsheet is provided by the authorized Telegram handler.
    """
    zone = ZoneInfo(local_timezone)
    instant = _aware_utc(clicked_at)
    try:
        tab = spreadsheet.worksheet(TAB)
    except gspread.exceptions.WorksheetNotFound:
        tab = spreadsheet.add_worksheet(
            title=TAB, rows=3000, cols=len(HEADERS)
        )
    rows = tab.get_all_values()
    if not rows:
        tab.append_row(list(HEADERS))
        rows = [list(HEADERS)]
    elif tuple(rows[0][:len(HEADERS)]) != HEADERS:
        raise ValueError("Unexpected bedtime sheet headers; no data changed")

    # Read persisted timestamps so deduplication survives service redeploys.
    # An out-of-order Telegram update still cannot create an accidental twin.
    for row in reversed(rows[1:]):
        if len(row) < len(HEADERS) or row[1] != EVENT_TYPE:
            continue
        try:
            existing = _aware_utc(datetime.fromisoformat(row[4]))
        except (TypeError, ValueError):
            continue
        if abs(instant - existing) < DUPLICATE_WINDOW:
            return False, existing.astimezone(zone)

    local = instant.astimezone(zone)
    key = "zhukov:bedtime:" + instant.strftime("%Y%m%dT%H%M%S.%fZ")
    tab.append_row([
        key, EVENT_TYPE, local.date().isoformat(),
        local.strftime("%H:%M:%S"), instant.isoformat(timespec="seconds"),
        getattr(zone, "key", str(zone)), "telegram:bedtime_button",
    ])
    return True, local
