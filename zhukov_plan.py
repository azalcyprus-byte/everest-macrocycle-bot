"""Read-only parser for Alexey's published Everest macrocycle daily plan.

Source: separate Google Sheet "Everest — Алексей Жуков",
tab "Макроцикл 2", columns "Дата", "Day Type".
Dates in that sheet are DD.MM without the year; the cycle year is explicit.
Do not infer a plan for dates absent from the source.
"""
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import os
import re

_ALLOWED_CODE = re.compile(r"^[A-Z][A-Z0-9+/-]{0,7}$")
DEFAULT_PLAN_SHEET = "Макроцикл 2"
CYCLE_START = date(2026, 11, 23)
CYCLE_END = date(2026, 12, 23)
DAILY_COLUMNS = {
    "game": "Game OPT (ч)",
    "heavy": "Анализ трансляций (мин)",
    "light": "Видеоразборы (мин)",
    "physical": "Физическая активность (мин)",
}


class PlannedLoadCode(str):
    """A day-type string with the optional Game OPT hours for that same date.

    As a str subclass it preserves existing monitoring code that expects
    a plain 'O', 'L', 'M', 'T', 'L+' code.
    """

    def __new__(cls, code, game_opt_hours=None):
        instance = super().__new__(cls, code)
        instance.game_opt_hours = game_opt_hours
        return instance


def _parse_hours(raw):
    """Parse only plausible nonnegative planned game hours, never guess them."""
    text = str(raw or "").strip().replace(",", ".")
    if not text:
        return None
    try:
        hours = Decimal(text)
    except InvalidOperation:
        return None
    if not hours.is_finite() or hours < 0 or hours > 24:
        return None
    return hours


def planned_opt_hours(plan_type):
    """Show what Game OPT states, retaining 0 for true non-playing days."""
    return getattr(plan_type, "game_opt_hours", None)


def parse_plan_rows(rows, *, year=2026):
    """Return date -> exact planned load code from the real dated plan."""
    if not isinstance(year, int) or not 2000 <= year <= 2100:
        raise ValueError("A valid explicit cycle year is required")
    if not rows:
        return {}
    headers = [str(x).strip() for x in rows[0]]
    if "Дата" not in headers or "Day Type" not in headers:
        raise ValueError("Plan tab must contain Дата and Day Type columns")
    date_col = headers.index("Дата")
    type_col = headers.index("Day Type")
    opt_col = headers.index("Game OPT (ч)") if "Game OPT (ч)" in headers else None
    result = {}
    conflicting = set()
    for row in rows[1:]:
        if len(row) <= max(date_col, type_col):
            continue
        raw = str(row[date_col]).strip()
        code = str(row[type_col]).strip().upper()
        if not _ALLOWED_CODE.fullmatch(code):
            continue
        day = None
        for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d.%m"):
            try:
                parsed = datetime.strptime(raw, fmt).date()
                day = parsed.replace(year=year) if fmt == "%d.%m" else parsed
                break
            except ValueError:
                continue
        if day is None:
            continue
        opt_hours = (
            _parse_hours(row[opt_col])
            if opt_col is not None and opt_col < len(row)
            else None
        )
        entry = PlannedLoadCode(code, opt_hours)
        if (day in result
                and (result[day] != entry
                     or planned_opt_hours(result[day]) != opt_hours)):
            conflicting.add(day)
        else:
            result[day] = entry
    for day in conflicting:
        result.pop(day, None)
    return result


def planned_type_for(plan, target):
    """Return code only for an explicitly listed date, never extrapolate."""
    if not isinstance(target, date):
        return None
    value = (plan or {}).get(target)
    return value if isinstance(value, str) and _ALLOWED_CODE.fullmatch(value) else None


def load_plan_types():
    """Fetch the official plan using the bot's existing read-only Sheets access."""
    import json
    import logging
    import os
    import threading
    import time

    import gspread
    from google.oauth2.service_account import Credentials

    global _cache, _cache_time
    with _cache_lock:
        now = time.monotonic()
        if _cache is not None and now - _cache_time < 600:
            return dict(_cache)

        credential_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
        if not credential_json:
            raise RuntimeError("Bot's Sheets credentials are not configured")
        spreadsheet_id = os.getenv(
            "ZHUKOV_PLAN_SPREADSHEET_ID",
            "1zhViAYswrLlOQJltqkZYRO8cNrxrdHtBkVNs_tHRbr8",
        )
        if not spreadsheet_id:
            raise RuntimeError("Zhukov macrocycle plan spreadsheet ID is missing")
        year = int(os.getenv("ZHUKOV_PLAN_YEAR", "2026"))
        credentials = Credentials.from_service_account_info(
            json.loads(credential_json),
            scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
        )
        client = gspread.authorize(credentials)
        plan_sheet = os.getenv(
            "ZHUKOV_PLAN_SHEET", DEFAULT_PLAN_SHEET
        ).strip() or DEFAULT_PLAN_SHEET
        rows = client.open_by_key(spreadsheet_id).worksheet(
            plan_sheet
        ).get("A1:I250", value_render_option="FORMATTED_VALUE")
        parsed = parse_plan_rows(rows, year=year)
        _cache = parsed
        _cache_time = now
        if not parsed:
            logging.getLogger(__name__).warning(
                "Zhukov plan has no dated day-type entries"
            )
        return dict(parsed)


def optional_plan_types():
    """Plan failures never suppress health-monitoring or AI notifications."""
    try:
        return load_plan_types()
    except Exception:
        import logging
        logging.getLogger(__name__).exception(
            "Zhukov macrocycle plan unavailable; day code omitted"
        )
        return {}


def parse_daily_plan_rows(rows):
    """Macrocycle 2 only. Empty loads are unknown; conflicting dates fail closed."""
    if not rows:
        return {}
    headers = [str(value).strip() for value in rows[0]]
    if "Дата" not in headers or "Day Type" not in headers:
        raise ValueError("Macrocycle 2 must contain Дата and Day Type")
    result, conflicts = {}, set()
    for row in rows[1:]:
        fields = {key: str(row[i] if i < len(row) else "").strip()
                  for i, key in enumerate(headers)}
        day = None
        for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d.%m"):
            try:
                day = datetime.strptime(fields["Дата"], fmt).date()
                if fmt == "%d.%m":
                    day = day.replace(year=2026)
                break
            except ValueError:
                continue
        if day is None or not CYCLE_START <= day <= CYCLE_END:
            continue
        entry = {
            "type": fields.get("Day Type") or None,
            "purpose": fields.get("Назначение дня") or None,
            "timezone": fields.get("Часовой пояс") or None,
        }
        for key, column in DAILY_COLUMNS.items():
            raw = fields.get(column, "")
            number = _parse_hours(raw) if key == "game" else _parse_minutes(raw)
            entry[key] = float(number) if number is not None else None
        if day in result and result[day] != entry:
            conflicts.add(day)
        else:
            result[day] = entry
    return {day: value for day, value in result.items() if day not in conflicts}


def _parse_minutes(raw):
    text = str(raw if raw is not None else "").strip().replace(",", ".")
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    return number if number.is_finite() and 0 <= number <= 1440 else None


def load_daily_plan():
    """Read all load categories from the exact cycle-2 sheet, with no fallback."""
    import json
    import time
    import gspread
    from google.oauth2.service_account import Credentials

    global _daily_cache, _daily_cache_time
    with _cache_lock:
        now = time.monotonic()
        if _daily_cache is not None and now - _daily_cache_time < 300:
            return {day: dict(value) for day, value in _daily_cache.items()}
        sheet_id = os.getenv("ZHUKOV_PLAN_SPREADSHEET_ID",
                             "1zhViAYswrLlOQJltqkZYRO8cNrxrdHtBkVNs_tHRbr8")
        title = os.getenv("ZHUKOV_PLAN_SHEET", DEFAULT_PLAN_SHEET).strip()
        if title != DEFAULT_PLAN_SHEET:
            raise ValueError("Morning dashboard requires the exact Макроцикл 2 tab")
        credentials = Credentials.from_service_account_info(
            json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]),
            scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
        )
        book = gspread.authorize(credentials).open_by_key(sheet_id)
        rows = book.worksheet(title).get("A1:M200", value_render_option="FORMATTED_VALUE")
        _daily_cache = parse_daily_plan_rows(rows)
        _daily_cache_time = now
        return {day: dict(value) for day, value in _daily_cache.items()}


import threading

_cache_lock = threading.Lock()
_cache = None
_cache_time = 0.0
_daily_cache = None
_daily_cache_time = 0.0
