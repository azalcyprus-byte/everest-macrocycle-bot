"""Everest: new all-days evening Google Form in a separate response spreadsheet.

The new form has a different schema from the legacy postgame form: it covers
training/rest days as well as game days. No synthetic old-form fields or GAS
events are created. Read and send only confirmed new submissions.
"""
from datetime import date, datetime, timedelta
import logging
import os
import re
from zoneinfo import ZoneInfo

import zhukov_plan

logger = logging.getLogger(__name__)
NEW_FORM_TAB = "Form Responses 1"
NEW_FORM_ID_ENV = "ZHUKOV_EVENING_GOOGLE_SHEET_ID"
ACTIVATION_KEY = "zhukov:evening-v2:activation"

# Fail closed if the form was accidentally pointed at the old source or
# its question set changes without updating the parser.
REQUIRED_HEADERS = {
    "Timestamp", "Дата",
    "Сегодня был игровой день?",
    "Что ощущалось в теле к концу дня?",
    "1. Сколько часов фактически играл сегодня?",
    "5. Были ли конкретные сбои внимания?",
}
TIMESTAMP_FORMATS = (
    "%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %I:%M %p",
    "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M",
    "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M",
    "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M",
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M:%S",
)


def _stamp(raw, tz):
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=tz)
    raw = str(raw or "").strip()
    try:
        iso = datetime.fromisoformat(raw)
        return iso if iso.tzinfo else iso.replace(tzinfo=tz)
    except ValueError:
        pass
    for fmt in TIMESTAMP_FORMATS:
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=tz)
        except ValueError:
            pass
    return None


def parse_records(rows, *, sheet_timezone=None):
    """Newest submitted response per explicit report date; retain original fields."""
    from zhukov_monitor import parse_date
    tz = ZoneInfo(sheet_timezone or os.getenv(
        "ZHUKOV_EVENING_SHEET_TIMEZONE", "Etc/GMT-3"
    ))
    if not rows or not rows[0]:
        return []
    headers = [str(h).strip() for h in rows[0]]
    if not REQUIRED_HEADERS.issubset(headers):
        missing = sorted(REQUIRED_HEADERS - set(headers))
        raise ValueError("New Everest evening form schema mismatch: " + repr(missing))
    latest = {}
    for values in rows[1:]:
        fields = {
            name: str(values[i] if i < len(values) and values[i] is not None else "").strip()
            for i, name in enumerate(headers) if name
        }
        stamp = _stamp(fields.get("Timestamp"), tz)
        day = parse_date(fields.get("Дата"))
        if stamp is None or day is None:
            # Unparseable submission must not generate a false report date.
            continue
        item = {"date": day, "timestamp": stamp, "fields": fields}
        current = latest.get(day)
        if current is None or stamp >= current["timestamp"]:
            latest[day] = item
    return sorted(latest.values(), key=lambda rec: rec["date"])


def _field(rec, prefix):
    for name, value in rec["fields"].items():
        if name.startswith(prefix):
            return _short(value)
    return "не указано"


def _short(value, limit=135):
    out = " ".join(str(value or "").split()).strip()
    return out[:limit] if out else "не указано"


def _minutes(rec, prefix):
    raw = _field(rec, prefix)
    try:
        val = float(raw.replace(",", "."))
    except ValueError:
        return None
    return val if 0 <= val <= 1440 else None


def _hours(rec):
    raw = _field(rec, "1. Сколько часов фактически")
    try:
        value = float(raw.replace(",", "."))
    except ValueError:
        return None
    return value if 0 <= value <= 24 else None


def _game_day(rec):
    answer = _field(rec, "Сегодня был игровой день?").casefold()
    if answer.startswith(("да", "yes", "игр")):
        return True
    if answer.startswith(("нет", "no", "неигр")):
        return False
    return None


def _flag(value):
    """Only affirmative, explicit answers count as a signal."""
    label = _short(value).casefold()
    if label in ("не указано", "нет данных", "нет", "не было", "no", "false", "0"):
        return False
    if label.startswith(("нет,", "нет ", "не было", "не замечал", "не отмеч")):
        return False
    return True


def render(record, morning, evenings, *, plan_type=None):
    """An evidence-based evening brief grounded in the new form's exact fields."""
    import zhukov_monitor as m

    day = record["date"]
    game = _game_day(record)
    game_hours = _hours(record)
    plan = zhukov_plan.planned_type_for({day: plan_type}, day)
    optimal = zhukov_plan.planned_opt_hours(plan) if plan else None
    planned = (
        f"{plan} ({str(optimal).replace('.', ',')} ч Game OPT)"
        if plan and optimal is not None else str(plan or "не указан")
    )
    same_day = next((r for r in morning if r["date"] == day), None)
    if same_day:
        bed = m.get(same_day, "1. Во сколько лег")
        woke = m.get(same_day, "2. Во сколько окончательно")
        asleep = m.get(same_day, "4. Как быстро уснул")
        awakenings = m.get(same_day, "3. Сколько раз просыпался")
        window = m.sleep_window(same_day)
        window_text = f"окно сна {window:g} ч" if window is not None else "окно сна неизвестно"
        morning_lines = (
            f"Лёг: {bed}; проснулся: {woke} ({window_text}, не фактический сон).\n"
            f"Засыпание: {asleep}; пробуждения: {awakenings}.\n"
            f"Сонливость утром: {m.get(same_day, '6. Через 30–60')}.\n"
            f"Трудности внимания утром: {m.get(same_day, '8. Когда читаешь')}."
        )
    else:
        morning_lines = "Утренней анкеты за эту дату нет; сон и состояние утром не оценены."

    game_label = (
        "игровой день" if game is True else
        "неигровой день" if game is False else
        "тип дня не указан"
    )
    activity = _field(record, "Чем ты занимался сегодня?")
    movement = _minutes(record, "Сколько минут физической активности")
    video = _minutes(record, "Сколько минут сегодня смотрел видеоразборы")
    active = _minutes(record, "Сколько минут сегодня занимался активным анализом")
    def fmt(val):
        return f"{val:g} мин" if val is not None else "не указано"
    body = _field(record, "Что ощущалось в теле к концу дня?")
    load = _field(record, "Насколько умственно тяжёлым")
    factors = _field(record, "Что сегодня могло повлиять")
    lapse = _field(record, "5. Были ли конкретные сбои внимания?")
    autopilot = _field(record, "4. Были ли моменты, когда принимал")
    signals = []
    if game is True and _flag(lapse):
        signals.append("сбои внимания")
    if game is True and _flag(autopilot):
        signals.append("привычные решения без полноценного анализа")
    if signals:
        status = "🟡 Сигналы для проверки: " + ", ".join(signals)
        recommendation = "Разобрать конкретные ситуации и сопоставить с утренним состоянием; план не меняется автоматически."
    else:
        status = "ℹ️ По заполненной анкете нет подтверждённых игровых сбоев" if game is True else "ℹ️ Игровые сбои не оцениваются без подтверждённой игры"
        recommendation = "Сопоставить вечернее самочувствие с утренним состоянием и следующей анкетой; решение по нагрузке остаётся за куратором."

    week = [x for x in evenings if day - timedelta(days=6) <= x["date"] <= day]
    three = [x for x in week if day - timedelta(days=2) <= x["date"]]
    def window(records):
        played = [x for x in records if _game_day(x) is True]
        known = [_hours(x) for x in played]
        known = [h for h in known if h is not None]
        return f"{len(records)} анкет; {len(played)} игровых дней; {sum(known):g} ч подтверждённой игры" if known else f"{len(records)} анкет; {len(played)} игровых дней; игровые часы не указаны"
    game_lines = ""
    if game is True:
        course = _field(record, "2. Менялось ли твоё состояние")
        attention = _field(record, "3. В первой трети")
        example = _field(record, "Короткий пример сбоя")
        extra = _field(record, "6. Если бы сейчас нужно было играть")
        game_lines = (
            "\n🎮 Во время игры\n"
            f"Факт: {str(game_hours).replace('.', ',') + ' ч' if game_hours is not None else 'не указано'} (со слов игрока).\n"
            f"Изменения состояния: {course}.\n"
            f"Внимание в первой трети: {attention}.\n"
            f"Автоматизм: {autopilot}.\n"
            f"Сбои: {lapse}; пример: {example}.\n"
        )
        # This question applies only to sessions of 8 hours or more.
        if game_hours is not None and game_hours >= 8 and extra != "не указано":
            game_lines += f"Готовность играть ещё 2 ч: {extra}.\n"

    return (
        f"🏔️ Everest | Алексей Жуков | {day:%d.%m.%Y}\n"
        f"Вечерний отчёт (новая Google Форма)\n\n"
        f"📅 План: {planned}; ответ игрока: {game_label}.\n"
        f"📋 Занятия: {activity}.\n"
        f"🏃 Активность: {fmt(movement)}; видеоразборы: {fmt(video)}; "
        f"активный анализ трансляций: {fmt(active)}.\n"
        f"🧠 Умственная нагрузка обучения: {load}.\n"
        f"💬 Тело к концу дня: {body}.\n"
        f"Факторы дня: {factors}.\n\n"
        f"💤 Сон и пробуждение (утренняя анкета)\n{morning_lines}\n"
        + game_lines
        + f"\n{status}\n\n"
        f"📈 Динамика вечерних анкет\n3 дня: {window(three)}.\n7 дней: {window(week)}.\n\n"
        f"🎯 Куратору: {recommendation}\n"
        f"ℹ️ Источник: новая вечерняя Google Форма; сон — утренняя анкета. "
        f"Описания субъективны, выводы предварительные."
    )


def historical_inputs(evening_spreadsheet, morning_spreadsheet, day):
    """Read-only retrospective from the new evening form, without future data."""
    if not isinstance(day, date):
        raise ValueError("Explicit report date required")
    import zhukov_monitor as m
    tab = evening_spreadsheet.worksheet(
        os.getenv("ZHUKOV_EVENING_RESPONSES_TAB", NEW_FORM_TAB)
    )
    rows = parse_records(tab.get_all_values())
    rows = [r for r in rows if r["date"] <= day]
    target = next((r for r in rows if r["date"] == day), None)
    if target is None:
        return None
    sheet_tz = ZoneInfo(os.getenv("ZHUKOV_SHEET_TIMEZONE", "Asia/Shanghai"))
    mtab = morning_spreadsheet.worksheet(
        os.getenv("ZHUKOV_MORNING_SHEET", "Утро — ответы")
    )
    morning = [r for r in m.normalize_records(mtab.get_all_values(), sheet_tz)
               if r["date"] <= day]
    return target, morning, rows


async def tick(bot, morning_spreadsheet, evening_spreadsheet, *, now=None, plan_types=None):
    """Read new form directly; leave legacy GAS monitoring and morning alerts alone."""
    import zhukov_monitor as m

    if not m.enabled():
        return
    if evening_spreadsheet is None:
        return
    async with m._lock:
        _, tz, _ = m.config()
        now = (now or datetime.now(tz)).astimezone(tz)
        tab = evening_spreadsheet.worksheet(
            os.getenv("ZHUKOV_EVENING_RESPONSES_TAB", NEW_FORM_TAB)
        )
        records = parse_records(tab.get_all_values())
        log_tab = m.worksheet(morning_spreadsheet)
        audit = log_tab.get_all_values()
        known = {r[0] for r in audit[1:] if r}
        activation = next((r for r in audit[1:] if r and r[0] == ACTIVATION_KEY), None)
        if activation and len(activation) > 3:
            started = datetime.fromisoformat(activation[3]).astimezone(tz)
        else:
            started = now
            log_tab.append_row([
                ACTIVATION_KEY, "CONFIG", now.date().isoformat(),
                now.isoformat(), "New evening source enabled; no historical flood.",
                "SENT", "", "",
            ])
            known.add(ACTIVATION_KEY)
        eligible = [
            r for r in records
            if r["timestamp"].astimezone(tz) >= started
            and r["timestamp"].astimezone(tz) <= now
            and f"zhukov:evening-analysis:{r['date'].isoformat()}" not in known
        ]
        if not eligible:
            return
        sheet_tz = ZoneInfo(os.getenv("ZHUKOV_SHEET_TIMEZONE", "Asia/Shanghai"))
        morning_tab = morning_spreadsheet.worksheet(
            os.getenv("ZHUKOV_MORNING_SHEET", "Утро — ответы")
        )
        morning = m.normalize_records(morning_tab.get_all_values(), sheet_tz)
        if plan_types is None:
            plan_types = await m.asyncio.to_thread(zhukov_plan.optional_plan_types)
        for rec in eligible:
            day = rec["date"]
            key = f"zhukov:evening-analysis:{day.isoformat()}"
            if key in known:
                continue
            msg = render(rec, morning, records,
                         plan_type=zhukov_plan.planned_type_for(plan_types, day))
            await m._send_once(bot, log_tab, known,
                               (key, "EVENING_ANALYSIS", day,
                                rec["timestamp"], msg, None))
