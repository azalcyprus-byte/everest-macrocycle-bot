"""Curator-only monitoring of Alexey Zhukov's existing Google Forms response sheets.

Disabled by default. Never sends monitored data to Alexey's own Telegram chat.
No changes to form schemas or to the curator's personal checklist sheets.
"""
import asyncio
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
import json
import logging
import os
import html
import re
import uuid
from zoneinfo import ZoneInfo

import gspread
import zhukov_ai
import zhukov_brief
import zhukov_plan
import aiohttp

logger = logging.getLogger(__name__)
LOG_TAB = "Everest_Алексей_мониторинг"
HEADERS = [
    "Ключ", "Тип", "Дата отчёта", "Контрольный момент ISO",
    "Сообщение", "Статус", "Ошибка", "Решение куратора",
]
_ENABLED = {"1", "true", "yes", "on"}
_lock = asyncio.Lock()


def enabled():
    return os.getenv("ZHUKOV_MONITOR_ENABLED", "").strip().lower() in _ENABLED


def config():
    # Form timestamps are displayed in the response spreadsheet's timezone.
    # The monitoring schedule must never depend on Alexey's location.
    sheet_tz = ZoneInfo(os.getenv("ZHUKOV_SHEET_TIMEZONE", "Asia/Shanghai"))
    tz = sheet_tz
    curator = os.getenv("ZHUKOV_CURATOR_CHAT_ID") or os.getenv("TELEGRAM_USER_ID")
    zhukov = os.getenv("ZHUKOV_TELEGRAM_USER_ID")
    if not curator or not curator.lstrip("-").isdigit():
        raise ValueError("A valid curator Telegram chat ID is required.")
    if zhukov and curator == zhukov:
        raise ValueError("Curator chat must not be Alexey's Telegram chat.")
    return tz, sheet_tz, int(curator)


def parse_date(raw):
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(str(raw or "").strip(), fmt).date()
        except ValueError:
            pass
    return None


def parse_stamp(raw, sheet_tz):
    raw = str(raw or "").strip()
    for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=sheet_tz)
        except ValueError:
            pass
    return None


def normalize_records(rows, sheet_tz):
    """Keep last submitted response by explicit report date, not submission date."""
    if not rows:
        return []
    headers = [str(x).strip() for x in rows[0]]
    result = {}
    for row in rows[1:]:
        fields = {name: str(row[i] if i < len(row) else "").strip()
                  for i, name in enumerate(headers) if name}
        stamp = parse_stamp(fields.get("Отметка времени"), sheet_tz)
        day = parse_date(fields.get("Дата")) or (stamp.date() if stamp else None)
        if day is None:
            continue
        item = {"date": day, "timestamp": stamp, "fields": fields}
        existing = result.get(day)
        if existing is None or ((stamp or datetime.min.replace(tzinfo=sheet_tz))
                                >= (existing["timestamp"] or datetime.min.replace(tzinfo=sheet_tz))):
            result[day] = item
    return sorted(result.values(), key=lambda item: item["date"])


def earliest_submission_by_day(rows, sheet_tz):
    """Keep earliest submission for deadline checks, independently of corrections."""
    if not rows:
        return {}
    headers = [str(x).strip() for x in rows[0]]
    result = {}
    for row in rows[1:]:
        fields = {name: str(row[i] if i < len(row) else "").strip()
                  for i, name in enumerate(headers) if name}
        stamp = parse_stamp(fields.get("Отметка времени"), sheet_tz)
        day = parse_date(fields.get("Дата")) or (stamp.date() if stamp else None)
        if day is None or stamp is None:
            continue
        if day not in result or stamp < result[day]:
            result[day] = stamp
    return result


def anchor_local(records, sheet_tz, activated, received_dates=None):
    """Return (first timestamp, reported date) after monitoring activation.

    Responses display timestamps in the Google Sheet timezone, irrespective of
    Alexey's current location. Never use archival responses to set the schedule.
    """
    override = os.getenv("ZHUKOV_MONITOR_ANCHOR_LOCAL", "").strip()
    if override:
        stamp = datetime.fromisoformat(override)
        stamp = stamp.replace(tzinfo=sheet_tz) if stamp.tzinfo is None else stamp.astimezone(sheet_tz)
        report_date = parse_date(os.getenv("ZHUKOV_MONITOR_ANCHOR_REPORT_DATE", "")) or stamp.date()
        return stamp, report_date

    candidates = [
        rec for rec in records
        if rec["date"] >= activated
        and rec["timestamp"] is not None
        and (received_dates is None or rec["date"] in received_dates)
    ]
    if not candidates:
        return None
    first = min(candidates, key=lambda rec: rec["timestamp"])
    return first["timestamp"].astimezone(sheet_tz), first["date"]


def due_checks(anchor, now, activation_date, anchor_report_date=None):
    """At T0+30 elapsed hours, and T0+30+24*n hours for subsequent reports.

    The report *date* comes from the form's "Дата" field, not from the local
    date of its deadline. Work in UTC for elapsed hours, even across DST.
    """
    if anchor is None:
        return []
    if anchor.tzinfo is None or now.tzinfo is None:
        raise ValueError("Schedule timestamps must include timezone information")
    anchor_report_date = anchor_report_date or anchor.date()
    first_due_utc = anchor.astimezone(timezone.utc) + timedelta(hours=30)
    now_utc = now.astimezone(timezone.utc)
    days_since_anchor = max(1, (activation_date - anchor_report_date).days)
    out = []
    index = days_since_anchor
    while True:
        due_utc = first_due_utc + timedelta(days=index - 1)
        if due_utc > now_utc:
            break
        report_date = anchor_report_date + timedelta(days=index)
        out.append((report_date, due_utc.astimezone(anchor.tzinfo)))
        index += 1
    return out


def get(rec, prefix, default="нет данных"):
    if not rec:
        return default
    for name, value in rec["fields"].items():
        if name.startswith(prefix):
            return value or default
    return default


def sleep_window(rec):
    try:
        a = datetime.strptime(get(rec, "1. Во сколько лег")[:5], "%H:%M")
        b = datetime.strptime(get(rec, "2. Во сколько окончательно")[:5], "%H:%M")
        diff = (b - a).total_seconds() / 3600
        diff = diff if diff >= 0 else diff + 24
        return round(diff, 1) if diff > 0 else None  # equal times are ambiguous
    except (TypeError, ValueError):
        return None


def game_hours(rec):
    try:
        h = float(get(rec, "1. Сколько часов фактически").replace(",", "."))
        return h if 0 <= h <= 24 else None
    except ValueError:
        return None


def automation(rec):
    text = get(rec, "5. Были ли моменты").lower()
    return "повторялось" in text or "один-два" in text


def concrete_lapse(rec):
    return get(rec, "6. Были ли конкретные сбои").lower() not in ("нет", "нет данных")


def quality_drop(rec):
    """Compare explicitly ordered questionnaire answers, not arbitrary readiness score."""
    attention = {"почти не следил": 0, "время от времени": 1,
                 "большую часть времени": 2, "почти всегда": 3}
    depth = {"практически нет": 0, "редко": 1, "выборочно": 2, "регулярно": 3}
    comparisons = (
        (get(rec, "3. В первой").lower(), get(rec, "3. В последней").lower(), attention),
        (get(rec, "4. В первой").lower(), get(rec, "4. В последней").lower(), depth),
    )
    result = []
    for before, after, scale in comparisons:
        result.append(None if before not in scale or after not in scale
                      else scale[after] < scale[before])
    return result


def stats(morning, games):
    windows = [sleep_window(x) for x in morning]
    hours = [game_hours(x) for x in games]
    drops = [quality_drop(x) for x in games]
    return {
        "mornings": len(morning),
        "games": len(games),
        "game_hours": round(sum(x for x in hours if x is not None), 1),
        "known_game_hours": sum(x is not None for x in hours),
        "sleep_windows": [x for x in windows if x is not None],
        "noticeable_sleepiness": sum("заметная" in get(x, "6. Через").lower() for x in morning),
        "attention_issue": sum(get(x, "8. Когда").lower() in ("иногда", "часто") for x in morning),
        "slower_decisions": sum(get(x, "9. Простые").lower() != "как обычно" for x in morning),
        "automatisms": sum(automation(x) for x in games),
        "lapses": sum(concrete_lapse(x) for x in games),
        "attention_drops": sum(x[0] is True for x in drops),
        "analysis_drops": sum(x[1] is True for x in drops),
    }


def fmt_stats(x, days):
    if not x["mornings"] and not x["games"]:
        return f"{days} дн.: нет наблюдений"
    win = x["sleep_windows"]
    sleep = f"; среднее окно сна {sum(win)/len(win):.1f} ч" if win else ""
    return (f"{days} дн.: утро {x['mornings']}/{days}, игра {x['games']} сессий, "
            f"{x['game_hours']} ч подтверждено в {x['known_game_hours']} сессиях"
            f"{sleep}; заметная сонливость {x['noticeable_sleepiness']}; "
            f"замедление решений {x['slower_decisions']}; автоматизм {x['automatisms']}; "
            f"конкретные сбои {x['lapses']}; снижение внимания/глубины анализа "
            f"{x['attention_drops']}/{x['analysis_drops']} сессий.")


def context_text():
    phase = os.getenv("ZHUKOV_MACROCYCLE_PHASE", "").strip()
    tournaments = os.getenv("ZHUKOV_PRIORITY_TOURNAMENTS", "").strip()
    return (f"Фаза: {phase or 'не указана'}; ближайшие приоритетные турниры: "
            f"{tournaments or 'не указаны'} (только сведения, заданные куратором).")



async def maybe_ai_commentary(report, record, morning, games, plan_type=None):
    """Optional individual hypotheses; never needed for reminders or data access."""
    return await zhukov_ai.enrich_report(
        report, record, morning, games,
        get, sleep_window, game_hours, automation, concrete_lapse, quality_drop,
        plan_type=plan_type,
    )


def report_text(record, morning, postgame, historical=False):
    day = record["date"]
    m3 = [x for x in morning if day - timedelta(days=2) <= x["date"] <= day]
    m7 = [x for x in morning if day - timedelta(days=6) <= x["date"] <= day]
    g3 = [x for x in postgame if day - timedelta(days=2) <= x["date"] <= day]
    g7 = [x for x in postgame if day - timedelta(days=6) <= x["date"] <= day]
    prev = next((x for x in reversed(morning) if x["date"] < day), None)
    previous_3 = [x for x in morning if day - timedelta(days=5) <= x["date"] < day - timedelta(days=2)]
    previous_7 = [x for x in morning if day - timedelta(days=13) <= x["date"] < day - timedelta(days=6)]
    g_cycle = [x for x in postgame if x["date"] <= day]
    macro_start = parse_date(os.getenv("ZHUKOV_MACROCYCLE_START_DATE", ""))
    m_cycle = [x for x in morning if x["date"] <= day and
               (macro_start is None or x["date"] >= macro_start)]
    g_cycle = [x for x in g_cycle if macro_start is None or x["date"] >= macro_start]
    today_win = sleep_window(record)
    rest = [x for x in m7 if get(x, "Сегодня").lower() == "отдых"]
    consecutive = any(
        (x["date"] - timedelta(days=1)) in {g["date"] for g in g7}
        for x in g7
    )
    long_prev = next(
        (x for x in reversed(postgame) if x["date"] == day - timedelta(days=1)
         and game_hours(x) is not None and game_hours(x) >= 9), None
    )
    today_game = next((x for x in postgame if x["date"] == day), None)
    fact = [
        f"Окно между отходом ко сну и подъёмом: {f'{today_win} ч' if today_win is not None else 'нет данных'}"
        " (не равняется фактическому сну).",
        f"Пробуждение: {get(record, '5. Как')}; сонливость через 30–60 минут: {get(record, '6. Через')}.",
        f"Тело: {get(record, '7. Что')}; внимание: {get(record, '8. Когда')}; "
        f"простые решения: {get(record, '9. Простые')}.",
        f"План дня: {get(record, 'Сегодня')}; "
        f"возможные факторы: {get(record, '11. Что со вчера')}.",
    ]
    if today_game:
        fact.append(f"Игра за дату отчёта: {game_hours(today_game)} ч; "
                    f"автоматизм: {get(today_game, '5. Были ли моменты')}; "
                    f"сбои: {get(today_game, '6. Были ли конкретные сбои')}.")
    if prev:
        fact.append(f"Относительно {prev['date'].strftime('%d.%m')}: сонливость "
                    f"{get(prev, '6. Через')} → {get(record, '6. Через')}; "
                    f"внимание {get(prev, '8. Когда')} → {get(record, '8. Когда')}.")
    else:
        fact.append("Нет предыдущего утреннего отчёта для сравнения.")
    latest = stats(m3, g3)
    week = stats(m7, g7)
    cycle = stats(m_cycle, g_cycle)
    notes = [
        fmt_stats(latest, 3),
        fmt_stats(week, 7),
        f"Весь наблюдаемый период ({len(m_cycle)} утренних и {len(g_cycle)} постигровых): "
        f"{cycle['game_hours']} ч игры в {cycle['known_game_hours']} сессиях; "
        f"автоматизм {cycle['automatisms']}, сбои {cycle['lapses']}.",
    ]
    if len(previous_3) >= 2 and len(m3) >= 2:
        notes.append("Сравнение 3 дней с предшествующими 3: "
                     f"заметная сонливость {stats(previous_3, [])['noticeable_sleepiness']}"
                     f" → {latest['noticeable_sleepiness']}, "
                     f"замедление решений {stats(previous_3, [])['slower_decisions']}"
                     f" → {latest['slower_decisions']} (число заполненных форм различается).")
    if len(previous_7) >= 4 and len(m7) >= 4:
        notes.append("Сравнение 7 дней с предыдущими 7: "
                     f"заметная сонливость {stats(previous_7, [])['noticeable_sleepiness']}"
                     f" → {week['noticeable_sleepiness']}.")
    if len(m7) < 4:
        notes.append("Для надёжной 7-дневной динамики данных недостаточно.")
    long_pairs = [
        (game, next_morning)
        for game in g7 if game_hours(game) is not None and game_hours(game) >= 9
        for next_morning in m7 if next_morning["date"] == game["date"] + timedelta(days=1)
    ]
    after_rest = [
        (rest_day, next_morning)
        for rest_day in rest
        for next_morning in m7 if next_morning["date"] == rest_day["date"] + timedelta(days=1)
    ]
    if long_pairs:
        next_sleepy = sum("заметная" in get(morn, "6. Через").lower()
                          for _, morn in long_pairs)
        notes.append(f"После игровых дней 9+ ч: {len(long_pairs)} утренних наблюдений, "
                     f"заметная сонливость {next_sleepy}; сравнение без доказательства причины.")
    else:
        notes.append("После игровых дней 9+ ч: недостаточно пар «игра → следующее утро».")
    if after_rest:
        normal = sum(get(morn, "9. Простые").lower() == "как обычно"
                     for _, morn in after_rest)
        notes.append(f"После дней отдыха: {len(after_rest)} наблюдений, "
                     f"обычный темп решений {normal}; эффект отдыха пока не установлен.")
    else:
        notes.append("Эффект дней отдыха: недостаточно пар «отдых → следующее утро».")
    notes.append(f"Последовательные игровые дни за 7 дней: {'есть' if consecutive else 'не подтверждены'}; "
                 f"утренних дней отдыха {len(rest)}; "
                 f"после игры 9+ ч накануне: {'да' if long_prev else 'нет подтверждения'}.")
    if historical:
        notes.append("Фаза макроцикла и приоритетные турниры на эту дату: "
                     "исторические данные не подключены; текущие настройки не переносим в прошлое.")
    else:
        notes.append(context_text())
    independent_flags = [
        get(record, "8. Когда").lower() == "часто",
        get(record, "9. Простые").lower() == "заметно тяжелее обычного",
        "признаки болезни" in get(record, "7. Что").lower(),
        bool(long_prev and (get(record, "9. Простые").lower() != "как обычно")),
    ]
    if sum(independent_flags) >= 2:
        proposal = ("Обсудить с куратором уменьшение дополнительной нагрузки, "
                    "использовать контроль внимания на перерывах; не менять обязательную "
                    "турнирную программу без отдельного решения.")
    elif "признаки болезни" in get(record, "7. Что").lower():
        proposal = "Учитывать признаки болезни и самочувствие отдельно от усталости; согласовать щадящий режим."
    else:
        proposal = ("Сохранить согласованный план с проверкой внимания на перерывах. "
                    "Нет оснований автоматически снижать нагрузку только из-за длительного сна или одной сонливости.")
    return (
        f"📊 Everest | Алексей Жуков | {day.strftime('%d.%m.%Y')}\n\n"
        "ФАКТ\n" + "\n".join(fact) + "\n\n"
        "НАБЛЮДЕНИЕ\n" + "\n".join(notes) + "\n\n"
        "ГИПОТЕЗА\nСонливость или длинное окно сна не доказывают накопленную усталость. "
        "Влияние длительности и последовательности игр, перелётов и отдыха проверяется на сопоставимых сессиях.\n\n"
        "ПРЕДЛАГАЕМОЕ РЕШЕНИЕ\n" + proposal
        + "\nРекомендация предварительная. Решение принимает куратор."
    )



def _historical_rows(spreadsheet):
    """Read Alexey's isolated history without creating or editing worksheets."""
    sheet_tz = ZoneInfo(os.getenv("ZHUKOV_SHEET_TIMEZONE", "Asia/Shanghai"))
    morning_tab = spreadsheet.worksheet(os.getenv("ZHUKOV_MORNING_SHEET", "Утро — ответы"))
    game_tab = spreadsheet.worksheet(os.getenv("ZHUKOV_POSTGAME_SHEET", "После игры — ответы"))
    morning = normalize_records(morning_tab.get_all_values(), sheet_tz)
    postgame = normalize_records(game_tab.get_all_values(), sheet_tz)
    try:
        audit = spreadsheet.worksheet(LOG_TAB).get_all_values()
    except gspread.exceptions.WorksheetNotFound:
        audit = [HEADERS]
    return morning, postgame, audit[1:]


def _audit_type(row):
    return str(row[1]).strip() if len(row) > 1 else ""


def _audit_day(row):
    return parse_date(row[2]) if len(row) > 2 else None


def _audit_decisions(audit_rows, day):
    """Confirmed curator decisions, independent of report delivery."""
    return [
        str(row[7] if len(row) > 7 and row[7] else row[4]).strip()
        for row in audit_rows
        if _audit_type(row) == "CURATOR_DECISION"
        and _audit_day(row) == day
        and len(row) > 4
    ]


def _audit_saved_report(audit_rows, day):
    """Return only a report that is confirmed sent to the curator."""
    for row in audit_rows:
        if (_audit_type(row) == "ANALYSIS" and _audit_day(row) == day
                and len(row) > 5 and row[5] == "SENT" and row[4]):
            return row[4]
    return None


def read_ai_report_inputs(spreadsheet, day, plan_types=None):
    """Read only historical Forms, without writing audit rows or calling OpenAI.

    The report text is generated retrospectively from data available through
    the selected day; future entries and current macrocycle context are excluded.
    Returns None when the requested day has no morning questionnaire.
    """
    if not isinstance(day, date):
        raise ValueError("A specific report date is required.")
    sheet_tz = ZoneInfo(os.getenv("ZHUKOV_SHEET_TIMEZONE", "Asia/Shanghai"))
    morning_tab = spreadsheet.worksheet(
        os.getenv("ZHUKOV_MORNING_SHEET", "Утро — ответы")
    )
    game_tab = spreadsheet.worksheet(
        os.getenv("ZHUKOV_POSTGAME_SHEET", "После игры — ответы")
    )
    morning = [
        row for row in normalize_records(morning_tab.get_all_values(), sheet_tz)
        if row["date"] <= day
    ]
    postgame = [
        row for row in normalize_records(game_tab.get_all_values(), sheet_tz)
        if row["date"] <= day
    ]
    target = next((row for row in morning if row["date"] == day), None)
    if target is None:
        return None
    return (zhukov_brief.render(target, morning, postgame, get,
                                ai_unavailable=True, plan_type=zhukov_plan.planned_type_for(plan_types, day)),
            target, morning, postgame)


def read_curator_report(spreadsheet, day=None, plan_types=None):
    """Return a saved sent report or an explicitly labelled historical reconstruction.

    Read-only by design. Do not send alerts, modify Forms, call OpenAI, or
    recreate historical context using today's macrocycle phase.
    """
    morning, postgame, audit = _historical_rows(spreadsheet)
    morning_by_day = {rec["date"]: rec for rec in morning}
    games_by_day = {rec["date"]: rec for rec in postgame}
    if day is None:
        available = sorted(
            set(morning_by_day)
            | set(games_by_day)
            | {_audit_day(row) for row in audit if _audit_type(row) in ("ANALYSIS", "MISSING")}
            - {None}
        )
        if not available:
            return "📚 Everest | Алексей Жуков\nПока нет ни одного отчёта."
        day = available[-1]
    if not isinstance(day, date):
        raise ValueError("Report date must be a date")
    date_label = day.strftime("%d.%m.%Y")
    notes = _audit_decisions(audit, day)
    saved = _audit_saved_report(audit, day)
    if saved:
        text = (
            f"💾 История Everest — {date_label}\n"
            "Оригинальная сохранённая сводка (отправлена куратору).\n\n"
            + saved
        )
    elif day in morning_by_day:
        text = (
            f"🔎 Архив Everest — {date_label}\n"
            "Ретроспективная сводка, рассчитанная сейчас по существующим анкетам. "
            "Не является копией прежнего Telegram-сообщения; данные могли быть исправлены позже.\n\n"
            + zhukov_brief.render(morning_by_day[day], morning, postgame,
                                          get, ai_unavailable=True,
                                          plan_type=zhukov_plan.planned_type_for(plan_types, day))
        )
    elif day in games_by_day:
        game = games_by_day[day]
        text = (
            f"🎲 Everest | Алексей Жуков | {date_label}\n\n"
            "Утреннего чек-листа за эту дату нет. Есть отчёт после игры:\n"
            f"Фактическая игра: {get(game, '1. Сколько часов фактически')} ч.\n"
            f"Автоматизм: {get(game, '5. Были ли моменты')}.\n"
            f"Конкретные сбои: {get(game, '6. Были ли конкретные сбои')}.\n\n"
            "Аналитику утреннего состояния без анкеты сформировать нельзя."
        )
    else:
        missed = [
            row for row in audit
            if _audit_type(row) == "MISSING" and _audit_day(row) == day
        ]
        if missed:
            text = (
                f"⚠️ Everest | Алексей Жуков | {date_label}\n"
                "В истории зафиксирован пропуск утреннего отчёта.\n\n"
                + (str(missed[0][4]) if len(missed[0]) > 4 else "")
            )
        else:
            text = (
                f"📭 Everest | Алексей Жуков | {date_label}\n"
                "Нет утренней анкеты, постигрового отчёта или сохранённой сводки "
                "за эту дату. Это не доказательство пропуска контрольного срока."
            )
    if notes:
        text += "\n\n✅ Решения куратора:\n" + "\n".join(
            f"— {item}" for item in notes
        )
    return text


def read_curator_history(spreadsheet, limit=15):
    """Compact index of real submitted dates and independently recorded misses."""
    limit = max(1, min(int(limit), 30))
    morning, postgame, audit = _historical_rows(spreadsheet)
    morning_dates = {item["date"] for item in morning}
    game_dates = {item["date"] for item in postgame}
    sent = {
        _audit_day(row) for row in audit
        if _audit_type(row) == "ANALYSIS" and len(row) > 5 and row[5] == "SENT"
    }
    missing = {_audit_day(row) for row in audit if _audit_type(row) == "MISSING"}
    decisions = {
        _audit_day(row) for row in audit if _audit_type(row) == "CURATOR_DECISION"
    }
    days = sorted(
        (morning_dates | game_dates | sent | missing | decisions) - {None},
        reverse=True,
    )[:limit]
    if not days:
        return "📚 Everest | Алексей Жуков\nИстория пока пуста."
    lines = [f"📚 Everest | Алексей Жуков\nПоследние {len(days)} дат с данными:\n"]
    for day in days:
        tags = []
        if day in sent:
            tags.append("💾 сохранённая сводка")
        elif day in morning_dates:
            tags.append("🔎 утренняя анкета")
        elif day in game_dates:
            tags.append("🎲 после игры")
        if day in missing:
            tags.append("⚠️ пропуск срока")
        if day in decisions:
            tags.append("✅ решение")
        lines.append(f"{day.strftime('%d.%m.%Y')} — {', '.join(tags)}")
    lines.extend([
        "",
        "Открыть день: /zhukovreport 20.10.2026",
        "Последний доступный: /zhukovreport",
        "Показать больше дат: /zhukovhistory 30",
        "",
        "🔎 — пересчёт из существующих анкет, 💾 — оригинальный сохранённый отчёт."
    ])
    return "\n".join(lines)


def telegram_report_html(text):
    """Escape report text; add safe Telegram formatting only to known headings.

    No AI output or questionnaire answer can inject Telegram HTML markup.
    """
    output = html.escape(str(text), quote=False)
    output = re.sub(
        r"(?m)^💳 OpenAI API:(?= ≈ \$| стоимость)",
        "💳 <b>OpenAI API</b>:",
        output,
    )
    output = re.sub(
        r"(?m)^💳 <b>OpenAI API</b>: ≈ (\$[0-9]+(?:\.[0-9]+)?)",
        r"💳 <b>OpenAI API</b>: ≈ <b>\1</b>",
        output,
    )
    # Compact brief title, objective status, and its fixed section headings.
    output = re.sub(
        r"(?m)^(🏔️ Everest \| Алексей Жуков \| \d{2}\.\d{2}\.\d{4})$",
        r"<b>\1</b>", output,
    )
    output = re.sub(
        r"(?m)^([✅🟡🟠] [^\n]+)$",
        r"<b>\1</b>", output,
    )
    output = re.sub(
        r"(?m)^(🔎 Значимые изменения|💡 Гипотеза|🎯 Рекомендация куратору)$",
        r"<b>\1</b>", output,
    )
    output = re.sub(
        r"(?m)^(ℹ️ Надёжность:)",
        r"<b>\1</b>", output,
    )
    # Exact labels are ours; all plan codes and form choices were HTML-escaped.
    output = re.sub(
        r"(?m)^(📅 План нагрузки:|🎮 Игровой день:)([^\n]*)$",
        r"<b>\1</b>\2", output,
    )
    return output


def split_telegram_text(text, max_chars=3400):
    """Split an arbitrarily long analysis without truncating its conclusion."""
    if max_chars < 250:
        raise ValueError("max_chars must be at least 250")
    chunks = []
    remaining = str(text)
    while remaining:
        if len(remaining) <= max_chars:
            chunks.append(remaining)
            break
        cut = remaining.rfind("\n", 0, max_chars + 1)
        if cut < max_chars // 3:
            cut = max_chars
            chunks.append(remaining[:cut])
            remaining = remaining[cut:]
        else:
            chunks.append(remaining[:cut])
            remaining = remaining[cut + 1:]
    return chunks


def worksheet(spreadsheet):
    try:
        return spreadsheet.worksheet(LOG_TAB)
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=LOG_TAB, rows=3000, cols=len(HEADERS))
        ws.append_row(HEADERS)
        return ws


def _mark(ws, row_number, status, error=""):
    ws.update(range_name=f"F{row_number}:G{row_number}",
              values=[[status, error[:400]]])


def record_curator_decision(spreadsheet, day, decision, curator_id):
    """Append (never overwrite) a curator's decision to the isolated audit journal."""
    if not isinstance(day, date) or not str(decision).strip():
        raise ValueError("Date and non-empty curator decision are required.")
    note = str(decision).strip()[:1500]
    now = datetime.now(ZoneInfo(os.getenv("ZHUKOV_SHEET_TIMEZONE", "Asia/Shanghai")))
    sheet = worksheet(spreadsheet)
    key = f"zhukov:decision:{day.isoformat()}:{uuid.uuid4().hex[:12]}"
    sheet.append_row([
        key, "CURATOR_DECISION", day.isoformat(), now.isoformat(),
        f"Куратор {curator_id}: {note}", "CONFIRMED", "", note,
    ])
    return key


async def _send_once(bot, ws, records, event):
    key, kind, day, due, text = event
    if key in records:
        return
    ws.append_row([key, kind, day.isoformat(), due.isoformat(), text,
                   "PENDING", "", ""])
    row_number = len(ws.get_all_values())
    records.add(key)
    try:
        _, _, chat_id = config()
        # AI reports may exceed Telegram's single-message limit.
        for part in split_telegram_text(text):
            await bot.send_message(
                chat_id=chat_id,
                text=telegram_report_html(part),
                parse_mode="HTML",
            )
    except Exception as exc:
        logger.exception("Zhukov monitor notification failed for %s", key)
        # Do not automatically retry ambiguous network failures: no duplicate alerts.
        _mark(ws, row_number, "REVIEW", f"{type(exc).__name__}: {exc}")
        return
    _mark(ws, row_number, "SENT")


async def tick(bot, spreadsheet, now=None, plan_types=None):
    if not enabled():
        return
    if plan_types is None and os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON"):
        plan_types = await asyncio.to_thread(zhukov_plan.optional_plan_types)
    async with _lock:
        tz, sheet_tz, _ = config()
        now = (now or datetime.now(tz)).astimezone(tz)
        morning_tab = spreadsheet.worksheet(os.getenv("ZHUKOV_MORNING_SHEET", "Утро — ответы"))
        game_tab = spreadsheet.worksheet(os.getenv("ZHUKOV_POSTGAME_SHEET", "После игры — ответы"))
        morning_rows = morning_tab.get_all_values()
        morning = normalize_records(morning_rows, sheet_tz)
        first_submissions = earliest_submission_by_day(morning_rows, sheet_tz)
        postgame = normalize_records(game_tab.get_all_values(), sheet_tz)
        if not morning:
            logger.warning("Zhukov monitor: no dated morning reports")
            return
        # GAS source events are optional until the spreadsheet trigger is installed.
        # In script mode, event journal is the signal for new analytics.
        script_mode = os.getenv("ZHUKOV_MONITOR_EVENT_MODE", "poll").lower() == "script"
        received_dates = None
        if script_mode:
            try:
                event_tab = spreadsheet.worksheet("Everest_Алексей_события")
                entries = event_tab.get_all_values()
                received_dates = {
                    d for row in entries[1:]
                    if len(row) >= 3 and row[1] == morning_tab.title
                    for d in [parse_date(row[2])] if d is not None
                }
            except gspread.exceptions.WorksheetNotFound:
                logger.error("Zhukov Apps Script event tab is missing; analytics paused")
                received_dates = set()
        log_tab = worksheet(spreadsheet)
        log_rows = log_tab.get_all_values()
        known = {row[0] for row in log_rows[1:] if row}
        activated = next(
            (parse_date(row[2]) for row in log_rows[1:]
             if row and row[0] == "zhukov:monitor:activation"), None
        )
        if not activated:
            activated = now.date()
            log_tab.append_row(["zhukov:monitor:activation", "CONFIG",
                                activated.isoformat(), now.isoformat(),
                                "First enabled date; no historical missed-day flood.",
                                "SENT", "", ""])
            known.add("zhukov:monitor:activation")
        anchor_record = next(
            (row for row in log_rows[1:]
             if row and row[0] == "zhukov:monitor:anchor"), None
        )
        if anchor_record and len(anchor_record) > 3:
            saved = datetime.fromisoformat(anchor_record[3])
            ref = saved.astimezone(tz)
            anchor_report_date = parse_date(anchor_record[2])
            if anchor_report_date is None:
                logger.error("Zhukov monitor: stored anchor report date is invalid")
                return
        else:
            selected_anchor = anchor_local(morning, sheet_tz, activated, received_dates)
            if selected_anchor:
                ref, anchor_report_date = selected_anchor
                log_tab.append_row(["zhukov:monitor:anchor", "CONFIG",
                                    anchor_report_date.isoformat(), ref.isoformat(),
                                    "Fixed source-sheet timestamp for 30h +24h checks.",
                                    "SENT", "", ""])
            else:
                ref = None
        if not ref:
            logger.info("Zhukov monitoring: waiting for the first post-activation report")
            return
        # Deadline history uses the earliest submission, not a later correction.
        events = []
        for day, deadline in due_checks(ref, now, activated, anchor_report_date):
            first = first_submissions.get(day)
            late = first is not None and first.astimezone(tz) > deadline
            if first is None or late:
                qualifier = (
                    "Отчёт заполнен после срока; опорный график неизменен."
                    if late else "Отчёт пока не поступил."
                )
                events.append((
                    f"zhukov:missing:{day.isoformat()}", "MISSING",
                    day, deadline,
                    f"⚠️ Everest | Алексей Жуков\nНа контрольный момент "
                    f"{deadline.strftime('%d.%m.%Y %H:%M')} (время Google Таблицы: {getattr(tz,'key',str(tz))}) "
                    f"утреннего отчёта за {day.strftime('%d.%m.%Y')} не было. {qualifier}",
                ))
        for record in morning:
            day = record["date"]
            if day < activated or not record["timestamp"]:
                continue
            if received_dates is not None and day not in received_dates:
                continue
            if record["timestamp"].astimezone(tz) > now:
                continue
            # One curator analysis per day. A late form is still analyzed.
            events.append((f"zhukov:analysis:{day.isoformat()}", "ANALYSIS",
                           day, record["timestamp"], zhukov_brief.render(
                               record, morning, postgame, get, ai_unavailable=True,
                               plan_type=zhukov_plan.planned_type_for(plan_types, day),
                           )))
        for key, kind, day, when, message in sorted(events, key=lambda x: (x[2], x[1])):
            if key in known:
                continue
            if kind == "ANALYSIS":
                target = next((r for r in morning if r["date"] == day), None)
                if target is not None:
                    message = await maybe_ai_commentary(
                        message, target, morning, postgame,
                        plan_type=zhukov_plan.planned_type_for(plan_types, day),
                    )
            await _send_once(bot, log_tab, known, (key, kind, day, when, message))
