"""Deterministic morning curator report and PNG chart for Alexey Zhukov."""
from datetime import timedelta
from io import BytesIO
import math

from PIL import Image, ImageDraw, ImageFont

import zhukov_plan


def _choice(record, prefix, get):
    return get(record, prefix, "").strip().lower()


def _sleep_window(record, get):
    start = _clock_minutes(record, get)
    raw = _choice(record, "2. Во сколько окончательно", get)[:5]
    try:
        hour, minute = (int(x) for x in raw.split(":"))
        end = hour * 60 + minute
    except (ValueError, TypeError):
        return None
    if start is None or not 0 <= hour < 24 or not 0 <= minute < 60:
        return None
    duration = (end - start) % 1440
    return duration / 60 if duration else None


def condition_components(record, get):
    """Everest 100-point model: S(25) + Z(10) + V(35) + C(30)."""
    hours = _sleep_window(record, get)
    if hours is None:
        duration_score = 0
    elif 8 <= hours <= 10:
        duration_score = 15
    elif 7 <= hours < 8 or 10 < hours <= 11:
        duration_score = 12
    elif 6 <= hours < 7 or 11 < hours <= 12:
        duration_score = 8
    elif 5 <= hours < 6 or 12 < hours <= 13:
        duration_score = 4
    else:
        duration_score = 0

    awakenings = _choice(record, "3. Сколько раз", get)
    if awakenings == "не просыпался":
        awakening_score = 10
    elif awakenings in {"1 раз", "просыпался 1–2 раза, быстро засыпал"}:
        awakening_score = 8
    elif awakenings == "2 раза и более":
        awakening_score = 4
    elif "часто просыпался" in awakenings or "долго не мог уснуть" in awakenings:
        awakening_score = 2
    else:
        awakening_score = 0
    s_score = duration_score + awakening_score

    onset = _choice(record, "4. Как быстро уснул", get).replace("–", "-")
    if "до 15" in onset:
        z_score = 10
    elif "15-30" in onset:
        z_score = 8
    elif "30-60" in onset:
        z_score = 5
    elif onset and onset != "нет данных":
        z_score = 0
    else:
        z_score = 0

    rise_score = {
        "легко": 15, "обычно": 12, "тяжело": 6,
        "очень тяжело, хотелось продолжать спать": 0,
    }.get(_choice(record, "5. Как встал", get), 0)
    sleepiness_score = {
        "нет": 20, "немного": 12, "заметная, могу снова уснуть": 0,
    }.get(_choice(record, "6. Через", get), 0)
    v_score = rise_score + sleepiness_score

    attention_score = {
        "нет": 10, "иногда": 6, "часто": 0,
    }.get(_choice(record, "8. Когда", get), 0)
    decisions_score = {
        "как обычно": 10, "немного медленнее обычного": 6,
        "заметно тяжелее обычного": 0,
    }.get(_choice(record, "9. Простые", get), 0)
    body = _choice(record, "7. Что", get)
    if body == "ничего необычного":
        body_score = 10
    elif "признаки болезни" in body:
        body_score = 0
    elif "слабость" in body:
        body_score = 3
    elif body and body != "нет данных":
        body_score = 5
    else:
        body_score = 0
    c_score = attention_score + decisions_score + body_score
    return {"S": s_score, "Z": z_score, "V": v_score, "C": c_score}


def condition_index(record, get):
    return sum(condition_components(record, get).values())


def _clock_minutes(record, get):
    value = _choice(record, "1. Во сколько лег", get)[:5]
    try:
        hour, minute = (int(x) for x in value.split(":"))
    except (ValueError, TypeError):
        return None
    return hour * 60 + minute if 0 <= hour < 24 and 0 <= minute < 60 else None


def _clock_text(minutes):
    minutes = int(round(minutes)) % 1440
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def recent_bedtime(record, morning, get, limit=7):
    """Current bedtime, circular mean of prior reports, signed shortest delta."""
    current = _clock_minutes(record, get)
    previous = sorted(
        (r for r in morning if r["date"] < record["date"]),
        key=lambda r: r["date"], reverse=True,
    )[:limit]
    values = [v for v in (_clock_minutes(r, get) for r in previous) if v is not None]
    if current is None or not values:
        return current, None, None, len(values)
    angles = [2 * math.pi * value / 1440 for value in values]
    angle = math.atan2(sum(math.sin(x) for x in angles),
                       sum(math.cos(x) for x in angles))
    average = ((angle % (2 * math.pi)) * 1440 / (2 * math.pi)) % 1440
    delta = (current - average + 720) % 1440 - 720
    return current, average, delta, len(values)


def _duration_text(minutes):
    sign = "+" if minutes >= 0 else "−"
    value = int(round(abs(minutes)))
    return f"{sign}{value // 60}ч{value % 60:02d}м"


def _hours_text(hours):
    minutes = int(round(hours * 60))
    return f"{minutes // 60}ч{minutes % 60:02d}м"


def recent_sleep_window(record, morning, get, limit=7):
    current = _sleep_window(record, get)
    previous = sorted(
        (r for r in morning if r["date"] < record["date"]),
        key=lambda r: r["date"], reverse=True,
    )[:limit]
    values = [value for value in (_sleep_window(r, get) for r in previous)
              if value is not None]
    average = sum(values) / len(values) if values else None
    return current, average, len(values)


def _risk(record, get):
    if "признаки болезни" in _choice(record, "7. Что", get):
        return "признаки недомогания"
    if "заметная" in _choice(record, "6. Через", get):
        return "выраженная сонливость после пробуждения"
    if _choice(record, "9. Простые", get) == "заметно тяжелее обычного":
        return "замедление простых решений"
    if _choice(record, "8. Когда", get) == "часто":
        return "частые трудности с удержанием внимания"
    body = _choice(record, "7. Что", get)
    if body not in {"", "нет данных", "ничего необычного"}:
        return body
    return ("не выявлен — в ответах нет выраженной сонливости, проблем "
            "внимания, замедления решений или недомогания")


def render_report(record, morning, get, plan_type, recommendation):
    components = condition_components(record, get)
    score = sum(components.values())
    yesterday = record["date"] - timedelta(days=1)
    prior = next((r for r in morning if r["date"] == yesterday), None)
    delta = None if prior is None else score - condition_index(prior, get)
    comparison = "нет анкеты за вчера" if delta is None else f"изменение {delta:+d} баллов к вчера"
    hours = zhukov_plan.planned_opt_hours(plan_type)
    if plan_type is None:
        plan = "не указан"
    elif hours is None:
        plan = str(plan_type) + " · часы не указаны"
    else:
        plan = str(plan_type) + " · " + format(hours.normalize(), "f").replace(".", ",") + " ч"
    current, average, bedtime_delta, count = recent_bedtime(record, morning, get)
    window, average_window, window_count = recent_sleep_window(
        record, morning, get,
    )
    if current is None:
        sleep = "время отхода ко сну не распознано"
    elif average is None:
        sleep = f"лёг {_clock_text(current)}; недавнего среднего пока нет"
    else:
        sleep = (f"лёг {_clock_text(current)}; среднее предыдущих {count} анкет "
                 f"{_clock_text(average)}; отклонение {_duration_text(bedtime_delta)}")
    if window is None:
        sleep_window_text = "окно сна не рассчитано"
    elif average_window is None:
        sleep_window_text = f"окно сна {_hours_text(window)}; среднего пока нет"
    else:
        sleep_window_text = (
            f"окно сна {_hours_text(window)}; среднее предыдущих "
            f"{window_count} анкет {_hours_text(average_window)}"
        )
    return (
        f"🏔️ Everest | Алексей Жуков | {record['date']:%d.%m.%Y}\n"
        f"🌅 Индекс утра: {score}/100 "
        f"(S {components['S']} + Z {components['Z']} + V {components['V']} + C {components['C']}).\n"
        f"🔎 Значимые изменения: {comparison}.\n"
        f"📅 План: {plan}.\n"
        f"🎯 Рекомендация куратору: {recommendation}\n"
        f"⚠️ Ключевой риск по утренней анкете: {_risk(record, get)}.\n"
        f"🌙 Режим сна: {sleep}; {sleep_window_text}."
    )


def chart_series(record, morning, games, get, plan_types):
    dated_plan = sorted((d, p) for d, p in (plan_types or {}).items()
                        if d <= record["date"] and zhukov_plan.planned_opt_hours(p) is not None)
    if not dated_plan:
        return []
    start = dated_plan[0][0]
    days = []
    day = start
    morning_by_day = {r["date"]: r for r in morning}
    game_by_day = {r["date"]: r for r in games}
    plan_total = fact_total = 0.0
    while day <= record["date"]:
        planned = zhukov_plan.planned_opt_hours((plan_types or {}).get(day))
        if planned is not None:
            plan_total += float(planned)
        game = game_by_day.get(day)
        if game is not None:
            try:
                fact = float(get(game, "1. Сколько часов фактически").replace(",", "."))
            except ValueError:
                fact = None
            if fact is not None and 0 <= fact <= 24:
                fact_total += fact
        morning_record = morning_by_day.get(day)
        days.append({
            "date": day, "plan": plan_total, "fact": fact_total,
            "condition": None if morning_record is None else condition_index(morning_record, get),
        })
        day += timedelta(days=1)
    return days


def render_chart_png(series):
    if not series:
        raise ValueError("No dated plan values available for the chart")
    width, height = 1200, 680
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=20)
    small = ImageFont.load_default(size=16)
    left, top, right, bottom = 85, 70, 1115, 590
    plot_w, plot_h = right - left, bottom - top
    max_hours = max(1.0, max(max(x["plan"], x["fact"]) for x in series) * 1.12)
    draw.text((left, 22), "Everest: cumulative game load and morning condition", fill="#172033", font=font)
    for value in range(0, 101, 20):
        y = bottom - value / 100 * plot_h
        draw.line((left, y, right, y), fill="#e5e9f0", width=1)
        draw.text((20, y - 8), str(value), fill="#556070", font=small)
    for i in range(6):
        value = max_hours * i / 5
        y = bottom - i / 5 * plot_h
        draw.text((right + 10, y - 8), f"{value:.0f}h", fill="#556070", font=small)
    n = len(series)
    step = plot_w / max(1, n - 1)
    bar_w = max(18, min(54, int(plot_w / max(1, n) * .48)))
    plan_pts, fact_pts = [], []
    for i, item in enumerate(series):
        x = left + i * step if n > 1 else left + plot_w / 2
        if item["condition"] is not None:
            y = bottom - item["condition"] / 100 * plot_h
            draw.rounded_rectangle((x - bar_w / 2, y, x + bar_w / 2, bottom),
                                   radius=5, fill="#b9d7ff")
        plan_pts.append((x, bottom - item["plan"] / max_hours * plot_h))
        fact_pts.append((x, bottom - item["fact"] / max_hours * plot_h))
        draw.text((x - 22, bottom + 12), item["date"].strftime("%d.%m"), fill="#334155", font=small)
    if len(plan_pts) > 1:
        for a, b in zip(plan_pts, plan_pts[1:]):
            segments = 8
            for j in range(0, segments, 2):
                p1 = (a[0] + (b[0]-a[0])*j/segments, a[1] + (b[1]-a[1])*j/segments)
                p2 = (a[0] + (b[0]-a[0])*(j+1)/segments, a[1] + (b[1]-a[1])*(j+1)/segments)
                draw.line((p1, p2), fill="#f59e0b", width=5)
        draw.line(fact_pts, fill="#1769aa", width=6, joint="curve")
    draw.rectangle((left, top, right, bottom), outline="#8c97a8", width=2)
    draw.line((left, 620, left + 52, 620), fill="#1769aa", width=6)
    draw.text((left + 62, 609), "FACT", fill="#172033", font=small)
    for x in range(left + 180, left + 232, 14):
        draw.line((x, 620, x + 8, 620), fill="#f59e0b", width=5)
    draw.text((left + 242, 609), "PLAN", fill="#172033", font=small)
    draw.rectangle((left + 365, 608, left + 389, 632), fill="#b9d7ff")
    draw.text((left + 400, 609), "CONDITION 0-100", fill="#172033", font=small)
    out = BytesIO()
    image.save(out, format="PNG", optimize=True)
    out.seek(0)
    out.name = "everest_morning.png"
    return out
