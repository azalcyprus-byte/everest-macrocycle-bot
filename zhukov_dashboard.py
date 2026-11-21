"""Cycle-2 morning dashboard: explicit dates, independent loads, no imputation.

The historical Everest index formula remains in zhukov_morning. Missing inputs
are masked here rather than being displayed as a measured zero. Sleep latency
midpoints (7.5 / 22.5 / 45 minutes) were approved by the curator on 2026-11-21.
"""
from datetime import date, datetime, time, timedelta, timezone
from io import BytesIO
from pathlib import Path
import math
import os
import re
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

import zhukov_morning
import zhukov_plan
import zhukov_sleep

VERSION = "morning-dashboard-v2.1"
NO_DATA = "Нет данных"
CATEGORIES = (
    ("game", "Игра", "ч"),
    ("heavy", "Анализ трансляций", "мин"),
    ("light", "Видеоразборы", "мин"),
    ("physical", "Физическая активность", "мин"),
)
CITY_ZONES = {
    "пномпень": "Asia/Phnom_Penh", "phnom penh": "Asia/Phnom_Penh",
    "тайбэй": "Asia/Taipei", "тайпей": "Asia/Taipei", "taipei": "Asia/Taipei",
    "пекин": "Asia/Shanghai", "beijing": "Asia/Shanghai",
    "владивосток": "Asia/Vladivostok", "москва": "Europe/Moscow",
    "стамбул": "Europe/Istanbul", "бангкок": "Asia/Bangkok",
    "сеул": "Asia/Seoul", "токио": "Asia/Tokyo",
}


def _label(value):
    return re.sub(r"\s+", " ", re.sub(r"^\d+\.\s*", "", str(value))).strip()


def field(record, prefix, default=""):
    """Normalize question numbering only; preserve the actual answer verbatim."""
    wanted = _label(prefix).casefold()
    for key, value in (record or {}).get("fields", {}).items():
        if _label(key).casefold().startswith(wanted):
            return str(value if value is not None else "").strip() or default
    return default


def number(raw, maximum):
    try:
        value = float(str(raw).strip().replace(",", "."))
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and 0 <= value <= maximum else None


def _clock(raw):
    match = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)(?::[0-5]\d)?", str(raw).strip())
    return time(int(match[1]), int(match[2])) if match else None


def location_zone(record, plan=None):
    explicit = (plan or {}).get("timezone")
    city = field(record, "Город").casefold()
    name = explicit or CITY_ZONES.get(city)
    # An unknown stated city is not silently interpreted as Cambodia.
    if not name and not city:
        name = os.getenv("ZHUKOV_TIMEZONE", "Asia/Phnom_Penh")
    try:
        return ZoneInfo(name) if name else None
    except (KeyError, ValueError):
        return None


def latency_minutes(raw):
    label = re.sub(r"\s+", "", str(raw).lower()).replace("–", "-").replace("—", "-")
    if "до15" in label:
        return 7.5
    if "15-30" in label:
        return 22.5
    if "30-60" in label:
        return 45.0
    # Open-ended categories (over one hour) have no defensible midpoint.
    return None


def parse_bedtimes(rows):
    if not rows:
        return []
    if tuple(rows[0][:len(zhukov_sleep.HEADERS)]) != zhukov_sleep.HEADERS:
        raise ValueError("Unexpected bedtime registry schema")
    result = []
    for row in rows[1:]:
        if len(row) < 7 or row[1] != zhukov_sleep.EVENT_TYPE:
            continue
        try:
            stamp = datetime.fromisoformat(row[4])
        except ValueError:
            continue
        if stamp.tzinfo is not None:
            result.append(stamp.astimezone(timezone.utc))
    return sorted(set(result))


def estimated_sleep(record, bedtimes, plan=None):
    """Wake instant minus recorded button instant minus agreed latency.

    Never substitute the morning self-reported bedtime for a missing button.
    Use elapsed UTC time across DST. Unknown wake zone/latency/button => None.
    """
    zone = location_zone(record, plan)
    wake_time = _clock(field(record, "Во сколько окончательно проснулся"))
    latency = latency_minutes(field(record, "Как быстро уснул"))
    if zone is None or wake_time is None or latency is None:
        return None
    wake_local = datetime.combine(record["date"], wake_time, zone)
    # Reject nonexistent/ambiguous wall times rather than guessing a DST fold.
    wake = wake_local.astimezone(timezone.utc)
    if wake.astimezone(zone).replace(tzinfo=None) != wake_local.replace(tzinfo=None):
        return None
    if wake_local.replace(fold=0).utcoffset() != wake_local.replace(fold=1).utcoffset():
        return None
    submitted = record.get("timestamp")
    if submitted is not None and wake > submitted.astimezone(timezone.utc) + timedelta(minutes=5):
        return None
    candidates = [stamp for stamp in bedtimes if timedelta(0) < wake - stamp <= timedelta(hours=24)]
    if not candidates:
        return None
    minutes = (wake - max(candidates)).total_seconds() / 60 - latency
    return minutes / 60 if 0 < minutes <= 24 * 60 else None


def historical_index(record):
    required = ("Во сколько лег спать", "Во сколько окончательно проснулся",
                "Сколько раз просыпался", "Как быстро уснул", "Как встал",
                "Через 30–60", "Что сейчас", "Когда читаешь", "Простые")
    missing = {"", "нет данных", "не указано", "—", "n/a"}
    if record is None or any(field(record, name).casefold() in missing for name in required):
        return None
    if zhukov_morning._sleep_window(record, field) is None:
        return None
    return zhukov_morning.condition_index(record, field)


def evening_loads(record):
    if record is None:
        return dict.fromkeys(key for key, _, _ in CATEGORIES)
    game_answer = field(record, "Сегодня был игровой день?").casefold()
    game = number(field(record, "Сколько часов фактически"), 24)
    if game_answer.startswith(("нет", "no")):
        game = 0.0  # Explicitly reported no game is a real zero.
    elif not game_answer.startswith(("да", "yes")):
        game = None
    return {
        "game": game,
        "heavy": number(field(record, "Сколько минут сегодня занимался активным анализом"), 1440),
        "light": number(field(record, "Сколько минут сегодня смотрел видеоразборы"), 1440),
        "physical": number(field(record, "Сколько минут физической активности"), 1440),
    }


def _latest(records, day):
    candidates = [record for record in records if record["date"] == day]
    return max(candidates, key=lambda r: r.get("timestamp") or datetime.min.replace(tzinfo=timezone.utc)) if candidates else None


def _fmt(value, unit="ч"):
    return NO_DATA if value is None else f"{value:g}".replace(".", ",") + " " + unit


def _duration(value):
    if value is None:
        return NO_DATA
    minutes = round(value * 60)
    return f"{minutes // 60} ч {minutes % 60:02d} мин"


def build_report(record, mornings, evenings, bedtimes, plans, warnings=()):
    day = record["date"]
    if not zhukov_plan.CYCLE_START <= day <= zhukov_plan.CYCLE_END:
        raise ValueError("New dashboard requires a Macrocycle 2 date")
    yesterday = day - timedelta(days=1)
    # Explicit report dates are local calendar dates, never shifted by timestamp.
    mornings = [r for r in mornings if r["date"] <= day]
    evenings = [r for r in evenings if r["date"] < day]
    current_plan = plans.get(day, {})
    previous = _latest(mornings, yesterday)
    score, prior_score = historical_index(record), historical_index(previous)
    delta = score - prior_score if score is not None and prior_score is not None else None
    series = []
    for offset in range(6, -1, -1):
        target = day - timedelta(days=offset)
        series.append({"date": target, "index": historical_index(_latest(mornings, target))})
    yesterday_record = _latest(evenings, yesterday)
    facts = evening_loads(yesterday_record)
    three_days = [day - timedelta(days=offset) for offset in (3, 2, 1)]
    totals = {}
    for key, _, _ in CATEGORIES:
        known = [evening_loads(_latest(evenings, d))[key] for d in three_days]
        values = [value for value in known if value is not None]
        totals[key] = {"value": sum(values) if values else None, "days": len(values)}
    sleep_values = []
    for offset in (2, 1, 0):
        d = day - timedelta(days=offset)
        morning = _latest(mornings, d)
        value = estimated_sleep(morning, bedtimes, plans.get(d)) if morning else None
        if value is not None:
            sleep_values.append(value)
    zone = location_zone(record, current_plan)
    report = {
        "version": VERSION, "date": day, "yesterday": yesterday,
        "zone": zone.key if zone else "Не определён: укажите часовой пояс в плане",
        "index": score, "delta": delta, "series": series,
        "sleep": estimated_sleep(record, bedtimes, current_plan),
        "sleepiness": field(record, "Через 30–60", NO_DATA),
        "thinking": field(record, "Насколько легко тебе сейчас думать", NO_DATA),
        "awakenings": field(record, "Сколько раз просыпался", NO_DATA),
        "latency_answer": field(record, "Как быстро уснул", NO_DATA),
        "body": field(record, "Что сейчас", NO_DATA),
        "factors": field(record, "Что со вчера могло повлиять", NO_DATA),
        "facts": facts, "yesterday_plan": plans.get(yesterday, {}),
        "totals": totals,
        "average_sleep": sum(sleep_values) / len(sleep_values) if sleep_values else None,
        "sleep_nights": len(sleep_values), "today_plan": current_plan,
        "warnings": list(warnings),
        "learning_effort": field(yesterday_record, "Насколько умственно тяжёлым", NO_DATA),
        "evening_factors": field(yesterday_record, "Что сегодня могло повлиять", NO_DATA),
        "game_quality": [],
    }
    if field(yesterday_record, "Сегодня был игровой день?").casefold().startswith(("да", "yes")):
        report["game_quality"] = [
            "Состояние: " + field(yesterday_record, "Менялось ли твоё состояние", NO_DATA),
            "Внимание: " + field(yesterday_record, "В первой трети", NO_DATA),
            "Привычная линия: " + field(yesterday_record, "Были ли моменты, когда принимал", NO_DATA),
            "Сбои: " + field(yesterday_record, "Были ли конкретные сбои", NO_DATA),
        ]
    report["analysis"] = analysis(report, record)
    return report


def analysis(report, record):
    delta = report["delta"]
    comparison = ("Сравнение со вчера: нет полного индекса за оба утра."
                  if delta is None else f"Индекс к вчера: {delta:+d} балл(а).")
    load = "; ".join(f"{label}: {_fmt(report['totals'][key]['value'], unit)} "
                     f"({report['totals'][key]['days']}/3 дн.)"
                     for key, label, unit in CATEGORIES)
    signals = []
    if report["sleep"] is not None and report["sleep"] < 7:
        signals.append("расчётное время сна менее 7 ч")
    if "заметная" in report["sleepiness"].casefold():
        signals.append("выраженная сонливость")
    if field(record, "Когда читаешь").casefold() == "часто":
        signals.append("частые трудности внимания")
    if "тяжел" in report["thinking"].casefold() or "тяжёл" in report["thinking"].casefold():
        signals.append("затруднение сложного мышления")
    body = report["body"].casefold()
    if body not in ("ничего необычного", "нет", NO_DATA.casefold()):
        signals.append("отмечены телесные симптомы")
    if delta is not None and delta <= -10:
        signals.append("снижение исторического индекса")
    recovery = ("Возможное недостаточное восстановление: " + ", ".join(signals) + "."
                if signals else "По этим данным достаточность восстановления не установлена.")
    attention = ("Перед подтверждением плана уточнить симптомы и возможность уменьшить нагрузку."
                 if signals else "Перед подтверждением плана проверить сон, симптомы и субъективную готовность.")
    if any(value["days"] < 3 for value in report["totals"].values()) or report["sleep_nights"] < 3:
        attention += " Данные за 72 ч неполные."
    return [comparison, "За три дня: " + load + ".", recovery, attention]


def render_png(report, *, demo=False):
    """Phone-readable deterministic PNG, with bundled OFL Cyrillic typography."""
    width, height = 1080, 8000
    bg, card, ink, muted = "#08182D", "#112740", "#F4F7FC", "#B1C3D7"
    cyan, amber = "#53D9D2", "#F5BA61"
    image = Image.new("RGB", (width, height), bg)
    draw = ImageDraw.Draw(image)
    font_path = Path(__file__).parent / "assets" / "NotoSans.ttf"
    fonts = {}

    def font(size):
        if size not in fonts:
            fonts[size] = ImageFont.truetype(str(font_path), size)
        return fonts[size]

    def text(x, y, content, size=28, fill=ink):
        draw.text((x, y), str(content), font=font(size), fill=fill)

    def wrapped(content, max_width, size=27):
        words = str(content).split()
        lines, current = [], ""
        for word in words:
            candidate = (current + " " + word).strip()
            if draw.textlength(candidate, font=font(size)) > max_width and current:
                lines.append(current)
                current = word
            else:
                current = candidate
        return lines + ([current] if current else [])

    y = 34
    # Everest mountain mark is vector-drawn, not an external brand asset.
    draw.line([(52, 84), (81, 40), (111, 84), (94, 67), (81, 75), (70, 65)], fill=cyan, width=4)
    text(130, y, "EVEREST", 40)
    text(700, y + 7, report["date"].strftime("%d.%m.%Y"), 32)
    text(48, 104, "Алексей Жуков  /  Макроцикл 2  /  Утренний отчёт", 28, muted)
    if demo:
        text(48, 147, "ТЕСТОВЫЙ ПРИМЕР • ДАННЫЕ ВЫМЫШЛЕННЫЕ", 26, amber)
    y = 198 if demo else 162

    def start_card(title, card_height):
        nonlocal y
        draw.rounded_rectangle((32, y, 1048, y + card_height), radius=23, fill=card)
        text(56, y + 18, title, 32)
        return y + 74

    def end_card(card_height):
        nonlocal y
        y += card_height + 22

    state_lines = ["Сонливость: " + report["sleepiness"],
                   "Сложное мышление: " + report["thinking"],
                   "Засыпание: " + report["latency_answer"],
                   "Пробуждения: " + report["awakenings"],
                   "Тело: " + report["body"], "Факторы: " + report["factors"]]
    state_lines = [line for item in state_lines for line in wrapped(item, 960)]
    state_height = 236 + 37 * len(state_lines)
    top = start_card("01  Утреннее состояние", state_height)
    text(56, top - 10, str(report["index"]) if report["index"] is not None else "—", 70, cyan)
    text(183, top + 28, "/ 100", 30, muted)
    text(56, top + 84, "К вчера: " + (f"{report['delta']:+d}" if report["delta"] is not None else NO_DATA), 27, muted)
    text(470, top, _duration(report["sleep"]), 43, ink)
    text(470, top + 66, "Расчётное время сна", 26, muted)
    for i, line in enumerate(state_lines):
        text(56, top + 143 + i * 37, line)
    end_card(state_height)

    top = start_card("02  Динамика индекса · 7 календарных дней", 320)
    left, right, chart_top, bottom = 130, 1002, top + 15, top + 171
    for value in (0, 50, 100):
        yy = bottom - value / 100 * (bottom - chart_top)
        draw.line((left, yy, right, yy), fill="#2B425B", width=1)
        text(52, yy - 17, value, 21, muted)
    previous = None
    for i, point in enumerate(report["series"]):
        xx = left + i * (right - left) / 6
        value = point["index"]
        if value is None:
            text(xx - 8, bottom - 32, "—", 27, muted)
            previous = None
        else:
            yy = bottom - value / 100 * (bottom - chart_top)
            if previous:
                draw.line((*previous, xx, yy), fill=cyan, width=5)
            draw.ellipse((xx - 6, yy - 6, xx + 6, yy + 6), fill=cyan)
            text(xx - 15, yy - 32, value, 22)
            previous = (xx, yy)
        text(xx - 30, bottom + 16, point["date"].strftime("%d.%m"), 23, muted)
    end_card(320)

    effort = number(report["learning_effort"], 10)
    effort_text = (f"{effort:g}/10" if effort is not None else report["learning_effort"])
    extra = ["Тяжесть обучения (ответ): " + effort_text,
             "Факторы вечера: " + report["evening_factors"]] + report["game_quality"]
    extra = [line for item in extra for line in wrapped(item, 960, 25)]
    load_height = 450 + len(extra) * 34
    top = start_card("03  Вчера · " + report["yesterday"].strftime("%d.%m") + " · план / факт", load_height)
    text(650, top - 3, "ПЛАН", 23, amber)
    text(840, top - 3, "ФАКТ", 23, cyan)
    for i, (key, label, unit) in enumerate(CATEGORIES):
        yy = top + 44 + i * 76
        planned, actual = report["yesterday_plan"].get(key), report["facts"][key]
        text(56, yy, label, 27)
        plan_text = "Самоконтроль" if key == "physical" and planned is None else "План не задан"
        text(640, yy, _fmt(planned, unit) if planned is not None else plan_text, 24, amber)
        text(840, yy, _fmt(actual, unit), 24, cyan)
        maximum = max([v for v in (planned, actual) if v is not None] + [1])
        for val, color, shift in ((planned, amber, 40), (actual, cyan, 50)):
            if val is not None:
                draw.rounded_rectangle((57, yy + shift, 57 + 500 * val / maximum, yy + shift + 6), radius=2, fill=color)
    for i, line in enumerate(extra):
        text(56, top + 359 + 34 * i, line, 25, muted)
    end_card(load_height)

    top = start_card("04  Нагрузка за 72 ч · три завершённых дня", 355)
    for i, (key, label, unit) in enumerate(CATEGORIES):
        row, column = divmod(i, 2)
        xx, yy = 56 + column * 505, top + row * 92
        total = report["totals"][key]
        text(xx, yy, label, 25, muted)
        text(xx, yy + 35, _fmt(total["value"], unit) + f"  ·  {total['days']}/3 дн.", 30)
    text(56, top + 190, "Средний расчётный сон: " + _duration(report["average_sleep"]) + f" · {report['sleep_nights']}/3 ночи", 27, cyan)
    text(56, top + 231, "Объёмы не суммируются в балл усталости.", 24, muted)
    end_card(355)

    conclusion = [line for item in (report["analysis"][0], *report["analysis"][2:])
                  for line in wrapped(item, 960, 28)]
    # Exact 72-hour values and coverage already appear in the dedicated block.
    conclusion_height = 96 + 39 * len(conclusion)
    top = start_card("05  Вывод Everest", conclusion_height)
    for i, line in enumerate(conclusion):
        text(56, top + 39 * i, line, 28)
    end_card(conclusion_height)

    plan = report["today_plan"]
    today = ["Тип дня: " + (plan.get("type") or "План не задан")]
    for key, label, unit in CATEGORIES:
        val = plan.get(key)
        if key == "physical" and val is None:
            today.append(label + ": самоконтроль")
        else:
            today.append(label + ": " + (_fmt(val, unit) if val is not None else "План не задан"))
    if plan.get("purpose"):
        today.append(plan["purpose"])
    today = [line for item in today for line in wrapped(item, 960)]
    today_height = 98 + len(today) * 38
    top = start_card("06  План на сегодня", today_height)
    for i, line in enumerate(today):
        text(56, top + i * 38, line)
    end_card(today_height)

    footer = ["Часовой пояс: " + report["zone"],
              "Индекс — исторический показатель, не точная оценка игровой готовности.",
              "Сон: кнопка → пробуждение − засыпание; ночное бодрствование неизвестно."]
    footer.extend(report["warnings"])
    for item in footer:
        for line in wrapped(item, 980, 22):
            text(48, y, line, 22, muted)
            y += 32
    image = image.crop((0, 0, width, y + 25))
    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    output.seek(0)
    output.name = f"everest_morning_{report['date'].isoformat()}.png"
    return output
