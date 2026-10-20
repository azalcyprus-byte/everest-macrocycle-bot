"""Read-only parser for Alexey's published Everest macrocycle daily plan.

Source: separate Google Sheet "Everest — Алексей Жуков",
tab "Волна по дням", columns "Дата", "Day Type".
Dates in that sheet are DD.MM without the year; the cycle year is explicit.
Do not infer a plan for dates absent from the source.
"""
from datetime import date, datetime
import re

_ALLOWED_CODE = re.compile(r"^[A-Z][A-Z0-9+/-]{0,7}$")


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
        if day in result and result[day] != code:
            conflicting.add(day)
        else:
            result[day] = code
    for day in conflicting:
        result.pop(day, None)
    return result


def planned_type_for(plan, target):
    """Return code only for an explicitly listed date, never extrapolate."""
    if not isinstance(target, date):
        return None
    value = (plan or {}).get(target)
    return value if isinstance(value, str) and _ALLOWED_CODE.fullmatch(value) else None
