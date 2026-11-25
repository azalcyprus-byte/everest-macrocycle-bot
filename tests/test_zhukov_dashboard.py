import ast
import copy
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import gspread
from PIL import Image
from telegram.error import BadRequest

import zhukov_dashboard as dashboard
import zhukov_dashboard_delivery as delivery
import zhukov_plan as plan
import zhukov_sleep

DAY = date(2026, 11, 26)
ZONE = ZoneInfo("Asia/Phnom_Penh")


def morning(day=DAY, **changes):
    fields = {
        "Дата": day.strftime("%d.%m.%Y"), "Город": "Пномпень",
        "1. Во сколько лег спать?": "00:00",
        # Exact current form header uses 1., not the legacy 2.
        "1. Во сколько окончательно проснулся?": "08:30",
        "3. Сколько раз просыпался за ночь?": "не просыпался",
        "4. Как быстро уснул?": "До 15 минут",
        "5. Как встал?": "обычно", "6. Через 30–60 минут": "нет",
        "7. Что сейчас": "ничего необычного", "8. Когда читаешь": "нет",
        "9. Простые утренние дела": "как обычно",
        "12. Что со вчера могло повлиять": "ничего необычного",
    }
    fields.update(changes)
    return {"date": day, "timestamp": datetime.combine(day, datetime.min.time(), ZONE) + timedelta(hours=9), "fields": fields}


def evening(day=DAY - timedelta(days=1), game="Нет", hours="", heavy="60", light="30", physical="45"):
    return {"date": day, "timestamp": datetime.combine(day, datetime.min.time(), ZONE) + timedelta(hours=22), "fields": {
        "Сегодня был игровой день?": game, "1. Сколько часов фактически играл сегодня?": hours,
        "Сколько минут сегодня занимался активным анализом трансляций?": heavy,
        "Сколько минут сегодня смотрел видеоразборы?": light,
        "Сколько минут физической активности было сегодня?": physical,
        "Насколько умственно тяжёлым было сегодняшнее обучение?": "7",
        "Что сегодня могло повлиять на твоё состояние?": "длительное обучение",
        "2. Менялось ли твоё состояние по ходу игры?": "стабильное",
        "3. В первой трети дня: внимание": "почти всегда",
        "4. Были ли моменты, когда принимал первую привычную линию?": "Нет",
        "5. Были ли конкретные сбои внимания?": "Нет",
        "Timestamp": "11/25/2026 10:00:00 PM", "Дата": day.strftime("%m/%d/%Y"),
        "Что ощущалось в теле к концу дня?": "ничего необычного",
    }}


def bed(day=DAY):
    return datetime.combine(day, datetime.min.time(), ZONE).astimezone(timezone.utc)


def report(**kwargs):
    return dashboard.build_report(morning(), [morning()], [evening()], [bed()], {}, **kwargs)


class DashboardLogicTests(unittest.TestCase):
    def test_actual_headers_preserve_historical_index_formula(self):
        self.assertEqual(dashboard.historical_index(morning()), 97)
        incomplete = morning()
        incomplete["fields"]["5. Как встал?"] = ""
        self.assertIsNone(dashboard.historical_index(incomplete))

    def test_button_supplies_missing_bedtime_for_index_and_weekly_chart(self):
        current = morning(**{"1. Во сколько лег спать?": ""})
        self.assertIsNone(dashboard.historical_index(current))
        self.assertEqual(dashboard.historical_index(current, [bed()]), 97)
        result = dashboard.build_report(current, [current], [], [bed()], {})
        self.assertEqual(result["index"], 97)
        self.assertEqual(result["series"][-1]["index"], 97)
        self.assertAlmostEqual(result["sleep"], 8.375)

    def test_button_has_priority_over_obsolete_morning_bedtime(self):
        current = morning(**{"1. Во сколько лег спать?": "04:00"})
        self.assertEqual(dashboard.historical_index(current, [bed()]), 97)
        self.assertNotEqual(dashboard.historical_index(current), 97)

    def test_button_restores_two_day_index_comparison_when_form_omits_bedtime(self):
        prior = morning(DAY - timedelta(days=1), **{"1. Во сколько лег спать?": ""})
        current = morning(**{"1. Во сколько лег спать?": ""})
        report = dashboard.build_report(current, [prior, current], [], [bed(DAY - timedelta(days=1)), bed()], {})
        self.assertEqual(report["index"], 97)
        self.assertEqual(report["delta"], 0)
        self.assertEqual(report["series"][-2]["index"], 97)
        self.assertEqual(report["series"][-1]["index"], 97)

    def test_unknown_bedtime_is_not_scored_as_zero(self):
        current = morning(**{"1. Во сколько лег спать?": ""})
        self.assertIsNone(dashboard.historical_index(current, []))
        self.assertIsNone(dashboard.build_report(current, [current], [], [], {})["index"])

    def test_question_ten_is_ignored_in_all_state_assessments(self):
        current = morning()
        old_form = morning(**{
            "10. Насколько легко тебе сейчас думать и разбираться в сложных вопросах?":
                "Очень тяжело, хочется избегать умственной работы."
        })
        without_q10 = dashboard.build_report(current, [current], [], [], {})
        with_q10 = dashboard.build_report(old_form, [old_form], [], [], {})
        self.assertEqual(without_q10["index"], with_q10["index"])
        self.assertEqual(without_q10["analysis"], with_q10["analysis"])
        self.assertNotIn("thinking", with_q10)
        self.assertNotIn("затруднение сложного мышления", " ".join(with_q10["analysis"]))

    def test_sleep_button_midpoints_and_missing_inputs(self):
        for category, minutes in (("До 15 минут", 7.5), ("15–30 минут", 22.5), ("30–60 минут", 45)):
            current = morning(**{"4. Как быстро уснул?": category})
            self.assertAlmostEqual(dashboard.estimated_sleep(current, [bed()]), 8.5 - minutes / 60)
        self.assertIsNone(dashboard.estimated_sleep(morning(), []))
        self.assertIsNone(dashboard.estimated_sleep(morning(**{"4. Как быстро уснул?": "Более часа"}), [bed()]))
        self.assertIsNone(dashboard.estimated_sleep(morning(**{"Город": "неизвестный город"}), [bed()]))
        self.assertIsNone(dashboard.estimated_sleep(morning(**{"1. Во сколько окончательно проснулся?": ""}), [bed()]))

    def test_sleep_uses_utc_press_and_local_report_date_across_midnight(self):
        current = morning(**{"1. Во сколько окончательно проснулся?": "07:00"})
        marker = datetime(2026, 11, 25, 23, 30, tzinfo=ZONE).astimezone(timezone.utc)
        self.assertAlmostEqual(dashboard.estimated_sleep(current, [marker]), 7.375)
        self.assertIsNone(dashboard.estimated_sleep(current, [marker - timedelta(days=1)]))
        self.assertIsNone(dashboard.estimated_sleep(current, [marker + timedelta(days=1)]))

    def test_plan_timezone_resolves_unknown_city_and_dst_uses_elapsed_time(self):
        current = morning(**{"Город": "место не в справочнике"})
        self.assertAlmostEqual(dashboard.estimated_sleep(current, [bed()], {"timezone": "Asia/Phnom_Penh"}), 8.375)
        day = date(2026, 11, 1)
        current = morning(day, **{"Город": "New York", "1. Во сколько окончательно проснулся?": "08:00"})
        current["timestamp"] = datetime(2026, 11, 1, 9, tzinfo=ZoneInfo("America/New_York"))
        press = datetime(2026, 10, 31, 23, tzinfo=ZoneInfo("America/New_York"))
        self.assertAlmostEqual(dashboard.estimated_sleep(current, [press.astimezone(timezone.utc)],
                                                        {"timezone": "America/New_York"}), 9.875)

    def test_training_day_plan_fact_and_explicit_zero(self):
        plans = {DAY: {"type": "M", "heavy": 90, "light": 20},
                 DAY - timedelta(days=1): {"heavy": 60, "light": 30, "game": 0}}
        result = dashboard.build_report(morning(), [morning()], [evening()], [], plans)
        self.assertEqual(result["facts"], {"game": 0, "heavy": 60, "light": 30, "physical": 45})
        self.assertEqual(result["today_plan"]["heavy"], 90)
        self.assertIsNone(result["sleep"])
        self.assertEqual(result["game_quality"], [])

    def test_game_day_quality_and_new_evening_only_hours(self):
        result = dashboard.build_report(morning(), [morning()], [evening(game="Да", hours="8,5")], [], {})
        self.assertEqual(result["facts"]["game"], 8.5)
        self.assertEqual(len(result["game_quality"]), 4)

    def test_rest_travel_missing_numbers_do_not_become_zero(self):
        result = dashboard.build_report(morning(**{"Сегодня": "перелёт"}), [morning()],
                                        [evening(heavy="", light="", physical="")], [], {})
        self.assertEqual(result["facts"]["game"], 0)
        for key in ("heavy", "light", "physical"):
            self.assertIsNone(result["facts"][key])
            self.assertIsNone(result["totals"][key]["value"])
            self.assertEqual(result["totals"][key]["days"], 0)
        self.assertEqual(result["today_plan"], {})

    def test_72h_exact_three_completed_dates_and_three_nights(self):
        days = [DAY - timedelta(days=i) for i in range(5)]
        mornings = [morning(d) for d in days]
        evenings = [evening(d, "Да", str(10 - i)) for i, d in enumerate(days)]
        result = dashboard.build_report(mornings[0], mornings, evenings, [bed(d) for d in days], {})
        self.assertEqual(result["totals"]["game"], {"value": 24, "days": 3})
        self.assertEqual(result["sleep_nights"], 3)
        self.assertAlmostEqual(result["average_sleep"], 8.375)
        self.assertEqual(result["totals"]["heavy"]["value"], 180)

    def test_week_gaps_no_future_answers_or_zero_fill(self):
        future = morning(DAY + timedelta(days=1))
        result = dashboard.build_report(morning(), [morning(DAY - timedelta(days=2)), morning(), future], [], [], {})
        self.assertEqual(len(result["series"]), 7)
        self.assertIsNone(result["series"][-2]["index"])
        self.assertEqual(result["series"][-1]["index"], 97)
        self.assertIsNone(result["delta"])

    def test_good_index_never_asserts_no_overload(self):
        text = " ".join(report()["analysis"])
        self.assertIn("достаточность восстановления не установлена", text)
        self.assertNotIn("перегрузки нет", text)
        self.assertIn("неполные", text)

    def test_cycle2_parser_no_old_dates_conflicts_or_guessed_loads(self):
        rows = [["Дата", "Day Type", *plan.DAILY_COLUMNS.values(), "Часовой пояс"],
                ["26.11.2026", "M", "0", "90", "", "", "Asia/Phnom_Penh"],
                ["20.10.2026", "L", "5", "100", "20", "30"],
                ["24.11.2026", "L", "1", "30"], ["24.11.2026", "L", "2", "30"]]
        result = plan.parse_daily_plan_rows(rows)
        self.assertEqual(list(result), [DAY])
        self.assertEqual(result[DAY]["game"], 0)
        self.assertIsNone(result[DAY]["light"])
        self.assertEqual(result[DAY]["heavy"], 90)

    def test_png_renders_all_three_scenarios(self):
        for game, hours in (("Нет", ""), ("Да", "8"), ("Нет", "0")):
            result = dashboard.build_report(morning(), [morning()], [evening(game=game, hours=hours)], [], {})
            png = dashboard.render_png(result, demo=True)
            image = Image.open(png)
            self.assertEqual(image.format, "PNG")
            self.assertEqual(image.width, 1080)
            self.assertLess(image.height, 4096)


class Sheet:
    def __init__(self, title, rows):
        self.title, self.rows = title, copy.deepcopy(rows)
    def get_all_values(self):
        return copy.deepcopy(self.rows)
    def append_row(self, row):
        self.rows.append(list(row))
    def update(self, range_name, values):
        import re
        index = int(re.search(r"\d+", range_name)[0]) - 1
        self.rows[index] = list(values[0])


class Book:
    def __init__(self, tabs):
        self.tabs = {key: Sheet(key, rows) for key, rows in tabs.items()}
    def worksheet(self, title):
        if title not in self.tabs:
            raise gspread.exceptions.WorksheetNotFound(title)
        return self.tabs[title]
    def add_worksheet(self, title, rows, cols):
        self.tabs[title] = Sheet(title, [])
        return self.tabs[title]


class FakeBot:
    def __init__(self):
        self.sent, self.edits = [], []
        self.fail_send = False
    async def send_photo(self, **kwargs):
        self.sent.append(kwargs)
        if self.fail_send:
            raise TimeoutError("ambiguous delivery")
        return SimpleNamespace(message_id=123)
    async def edit_message_media(self, **kwargs):
        self.edits.append(kwargs)


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"ZHUKOV_MONITOR_ENABLED": "true", "TELEGRAM_USER_ID": "900",
                                           "ZHUKOV_CURATOR_CHAT_ID": "900", "ZHUKOV_TELEGRAM_USER_ID": "901"})
        self.env.start()
        self.addCleanup(self.env.stop)
        current = morning()
        headers = ["Отметка времени", *current["fields"]]
        row = ["26.11.2026 10:00:00", *current["fields"].values()]
        self.main = Book({"Утро — ответы": [headers, row]})
        item = evening()
        self.new = Book({"Form Responses 1": [list(item["fields"]), list(item["fields"].values())]})
        self.bot = FakeBot()
        self.now = datetime(2026, 11, 26, 11, tzinfo=ZONE)

    async def test_send_once_and_corrections_edit_same_message_after_restart(self):
        await delivery.tick(self.bot, self.main, self.new, now=self.now, plans={})
        await delivery.tick(self.bot, self.main, self.new, now=self.now, plans={})
        self.assertEqual(len(self.bot.sent), 1)
        # In-place edit without a new form submission timestamp is detected.
        self.main.tabs["Утро — ответы"].rows[1][-1] = "длительный перелёт"
        await delivery.tick(self.bot, self.main, self.new, now=self.now, plans={})
        self.assertEqual(len(self.bot.sent), 1)
        self.assertEqual(self.bot.edits[0]["message_id"], 123)
        self.assertEqual(self.bot.edits[0]["chat_id"], 900)
        await delivery.tick(self.bot, self.main, self.new, now=self.now, plans={})
        self.assertEqual(len(self.bot.edits), 1)

    async def test_evening_and_plan_corrections_refresh_png_not_new_message(self):
        await delivery.tick(self.bot, self.main, self.new, now=self.now, plans={})
        self.new.tabs["Form Responses 1"].rows[1][2] = "90"  # Heavy training minutes.
        await delivery.tick(self.bot, self.main, self.new, now=self.now, plans={DAY: {"heavy": 60}})
        self.assertEqual(len(self.bot.sent), 1)
        self.assertEqual(len(self.bot.edits), 1)

    async def test_ambiguous_send_is_not_retried(self):
        self.bot.fail_send = True
        await delivery.tick(self.bot, self.main, self.new, now=self.now, plans={})
        await delivery.tick(self.bot, self.main, self.new, now=self.now, plans={})
        self.assertEqual(len(self.bot.sent), 1)
        self.assertEqual(self.main.tabs[delivery.TAB].rows[-1][4], "REVIEW")

    async def test_missing_evening_source_does_not_read_legacy_sheet(self):
        await delivery.tick(self.bot, self.main, None, now=self.now, plans={})
        self.assertEqual(len(self.bot.sent), 1)
        self.assertNotIn("После игры — ответы", self.main.tabs)

    async def test_late_bedtime_registry_refreshes_same_message(self):
        await delivery.tick(self.bot, self.main, self.new, now=self.now, plans={})
        self.main.tabs[zhukov_sleep.TAB] = Sheet(zhukov_sleep.TAB, [list(zhukov_sleep.HEADERS),
            ["test", zhukov_sleep.EVENT_TYPE, "2026-11-26", "00:00:00", bed().isoformat(), ZONE.key, "telegram:bedtime_button"]])
        await delivery.tick(self.bot, self.main, self.new, now=self.now, plans={})
        self.assertEqual(len(self.bot.sent), 1)
        self.assertEqual(len(self.bot.edits), 1)

    async def test_deleted_message_never_creates_automatic_replacement(self):
        await delivery.tick(self.bot, self.main, self.new, now=self.now, plans={})
        async def cannot_edit(**kwargs):
            raise BadRequest("Message to edit not found")
        self.bot.edit_message_media = cannot_edit
        await delivery.tick(self.bot, self.main, self.new, now=self.now, plans={DAY: {"type": "L"}})
        self.assertEqual(len(self.bot.sent), 1)
        self.assertEqual(self.main.tabs[delivery.TAB].rows[-1][4], "REVIEW")

    async def test_future_date_cannot_send_or_backfill_cycle1(self):
        await delivery.tick(self.bot, self.main, self.new, now=self.now - timedelta(days=6), plans={})
        self.assertEqual(self.bot.sent, [])
        self.assertIn(delivery.TAB, self.main.tabs)

    def test_us_form_date_order_is_not_silently_reversed(self):
        tab = self.new.tabs["Form Responses 1"]
        tab.rows[1][tab.rows[0].index("Дата")] = "12/02/2026"
        _, records, _, _, _ = delivery.read_inputs(self.main, self.new, plans={})
        self.assertEqual(records[0]["date"], date(2026, 12, 2))
        self.assertNotIn("После игры — ответы", self.main.tabs)

    def test_production_disables_old_automatic_morning_picture(self):
        source = Path(__file__).resolve().parents[1].joinpath("bot.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute) and node.func.attr == "tick"
                 and isinstance(node.func.value, ast.Name) and node.func.value.id == "zhukov_monitor"]
        self.assertEqual(len(calls), 1)
        self.assertTrue(any(k.arg == "morning_reports" and k.value.value is False for k in calls[0].keywords))


if __name__ == "__main__":
    unittest.main()
