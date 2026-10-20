"""Independent curator-only monitoring of Zhukov's existing Forms responses.

No changes to Google Forms or to the curator's personal checklists.
The bot reads Sheets populated by the existing Forms/Apps Script pipeline.
"""
from datetime import datetime, date, timedelta, time
from zoneinfo import ZoneInfo
import hashlib
import json
import logging
import os
import re

log = logging.getLogger(__name__)
MONITOR_SHEET = "Everest_Алексей_мониторинг"
HEADERS = ["Ключ", "Тип", "Дата отчёта", "Время события ISO", "Текст", "Статус", "Ошибка"]
TIMEZONE = os.getenv("ZHUKOV_TIMEZONE", "Asia/Phnom_Penh")
CURATOR_CHAT = os.getenv("ZHUKOV_CURATOR_CHAT_ID") or os.getenv("TELEGRAM_USER_ID")
ENABLED = os.getenv("ZHUKOV_MONITOR_ENABLED", "false").lower() == "true"


def parse_date(raw):
    raw = str(raw or "").strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            pass
    return None


def parse_stamp(raw, tz):
    raw = str(raw or "").strip()
    for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=tz)
        except ValueError:
            pass
    return None


def normalize_records(rows, tz):
    if not rows:
        return []
    headers = [str(x).strip() for x in rows[0]]
    found = {}
    for row in rows[1:]:
        item = dict(zip(headers, list(row) + [""] * len(headers)))
        day = parse_date(item.get("Дата"))
        timestamp = parse_stamp(item.get("Отметка времени"), tz)
        if day is None and timestamp:
            day = timestamp.date()
        if day is None:
            continue
        # Late correction supersedes earlier submissions for the same report date.
        if day not in found or (timestamp and timestamp > (found[day]["timestamp"] or datetime.min.replace(tzinfo=tz))):
            found[day] = {"date": day, "timestamp": timestamp, "fields": item}
    return sorted(found.values(), key=lambda x: x["date"])


def due_days(anchor_timestamp, now):
    """Fixed local wall-clock 30h first check, then +24h; no drift on late forms."""
    if not anchor_timestamp:
        return []
    tz = anchor_timestamp.tzinfo
    first = anchor_timestamp + timedelta(hours=30)
    if now < first:
        return []
    result = []
    cursor = first
    while cursor <= now and len(result) < 500:
        result.append((cursor.date(), cursor))
        cursor = cursor + timedelta(days=1)
    return result


def field(record, prefix):
    if not record:
        return "нет данных"
    for key, value in record["fields"].items():
        if key.strip().startswith(prefix):
            return str(value or "не указано")
    return "нет данных"


def sleep_hours(record):
    if not record:
        return None
    try:
        a = field(record, "1. Во сколько лег")
        b = field(record, "2. Во сколько окончательно")
        start = datetime.strptime(a[:5], "%H:%M")
        end = datetime.strptime(b[:5], "%H:%M")
        diff = (end - start).total_seconds() / 3600
        return round(diff if diff >= 0 else diff + 24, 1)
    except (ValueError, TypeError):
        return None


def report_text(record, records, postgame_records=None, context=None):
    day = record["date"]
    recent3 = [r for r in records if day - timedelta(days=2) <= r["date"] <= day]
    recent7 = [r for r in records if day - timedelta(days=6) <= r["date"] <= day]
    earlier = next((r for r in reversed(records) if r["date"] < day), None)
    hours = sleep_hours(record)
    sleep_label = f"{hours} ч между отходом ко сну и подъёмом (не фактическая длительность сна)" if hours is not None else "не определено"
    daytime = field(record, "6. Через")
    attention = field(record, "8. Когда")
    decisions = field(record, "9. Простые")
    body = field(record, "7. Что")
    wake = field(record, "5. Как")
    day_type = record["fields"].get("Сегодня", "не указан")
    # Never interpret long sleep as daytime sleepiness or as proof of overload.
    facts = [
        f"Сон: {sleep_label}; пробуждение: {wake}.",
        f"Сонливость через 30–60 минут: {daytime}.",
        f"Тело: {body}; внимание: {attention}; простые решения: {decisions}.",
        f"План дня: {day_type}.",
    ]
    if earlier:
        facts.append(f"Предыдущий отчёт: {earlier['date'].isoformat()}, сонливость: {field(earlier, '6. Через')}, внимание: {field(earlier, '8. Когда')}.")
    else:
        facts.append("Предыдущего отчёта нет.")
    post = postgame_records or []
    game7 = [r for r in post if day - timedelta(days=6) <= r["date"] <= day]
    facts.append(f"Постигровых отчётов за 7 дней: {len(game7)}; без отдельного анализа раздач качество решений не подтверждено.")
    if context:
        facts.append(f"Контекст макроцикла: {context}.")
    else:
        facts.append("Фаза макроцикла и приоритетные турниры: данные не подключены.")
    note = (
        "Данных для устойчивой тенденции недостаточно."
        if len(recent7) < 4
        else "Есть серия наблюдений, но причинная связь с нагрузкой не установлена."
    )
    return (
        f"📊 Everest | Алексей Жуков | {day.strftime('%d.%m.%Y')}\n\n"
        "ФАКТ\n" + "\n".join(facts) + "\n\n"
        f"НАБЛЮДЕНИЕ\n3 дня: {len(recent3)}/3 отчётов; 7 дней: {len(recent7)}/7; весь период: {len(records)}. {note}\n\n"
        "ГИПОТЕЗА\nДлинный сон может отражать восстановление; сам по себе не доказывает перегрузку. "
        "Оценка автоматизма и глубины анализа раздач требует постигровых данных.\n\n"
        "ПРЕДЛАГАЕМОЕ РЕШЕНИЕ\nНе снижать нагрузку по длительности сна или одному признаку сонливости. "
        "Сопоставить внимание, самочувствие, игру и фазу макроцикла. Решение предварительное, требуется подтверждение куратора."
    )


def _worksheet(spreadsheet):
    try:
        return spreadsheet.worksheet(MONITOR_SHEET)
    except Exception as exc:
        # Only create if the sheet is genuinely absent.
        if exc.__class__.__name__ != "WorksheetNotFound":
            raise
        sheet = spreadsheet.add_worksheet(title=MONITOR_SHEET, rows=3000, cols=len(HEADERS))
        sheet.append_row(HEADERS)
        return sheet


async def tick(bot, spreadsheet, now=None):
    """Periodic polling; persistent keys prevent duplicate Telegram sends after restart.

    Delivery is at-least-once on ambiguous Telegram/network failures; pending rows
    are retained for diagnosis. Only curator chat receives reports.
    """
    if not ENABLED or not CURATOR_CHAT:
        return
    tz = ZoneInfo(TIMEZONE)
    now = now or datetime.now(tz)
    morning = spreadsheet.worksheet(os.getenv("ZHUKOV_MORNING_SHEET", "Утро — ответы"))
    records = normalize_records(morning.get_all_values(), tz)
    if not records:
        return
    ledger = _worksheet(spreadsheet)
    rows = ledger.get_all_values()
    known = {row[0] for row in rows[1:] if row}
    anchor = next((r["timestamp"] for r in records if r["timestamp"]), None)
    if not anchor:
        log.warning("Zhukov monitor: missing anchor timestamp")
        return
    latest = {r["date"]: r for r in records}
    events = []
    for day, due in due_days(anchor, now):
        if day not in latest:
            key = f"zhukov:missing:{day.isoformat()}"
            events.append((key, "MISSING", day, due, f"⚠️ Алексей Жуков: нет утреннего отчёта за {day.strftime('%d.%m.%Y')}. Контрольное время: {due.strftime('%H:%M')} ({TIMEZONE})."))
    for record in records:
        key = f"zhukov:analysis:{record['date'].isoformat()}:{hashlib.sha256(json.dumps(record['fields'],sort_keys=True,ensure_ascii=False).encode()).hexdigest()[:12]}"
        if record["date"] >= anchor.date() and record["timestamp"] and record["timestamp"] <= now:
            events.append((key, "ANALYSIS", record["date"], record["timestamp"], report_text(record, records)))
    # Backfill analysis only for latest 7 days; avoid flooding curator with historical data.
    for key, kind, day, when, message in events:
        if key in known or (kind == "ANALYSIS" and day < now.date() - timedelta(days=7)):
            continue
        ledger.append_row([key, kind, day.isoformat(), when.isoformat(), message, "PENDING", ""])
        known.add(key)
        try:
            await bot.send_message(chat_id=int(CURATOR_CHAT), text=message[:4096])
        except Exception as exc:
            log.exception("Zhukov curator notification failed: %s", key)
            continue
        # Mark sent in persistent sheet. If Telegram succeeded but sheet update
        # failed, PENDING remains; do not blindly resend ambiguous deliveries.
        ledger.update_cell(len(ledger.get_all_values()), 6, "SENT")
