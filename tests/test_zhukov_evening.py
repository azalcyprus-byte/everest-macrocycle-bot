import os
import unittest
from datetime import date, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

import gspread
import zhukov_evening as evening
import zhukov_monitor as monitor
from zhukov_plan import PlannedLoadCode

TZ = ZoneInfo("Asia/Shanghai")
HEAD = [
    "Отметка времени", "Дата",
    "1. Сколько часов фактически играл сегодня?",
    "2. Менялось ли твоё состояние по ходу игры?",
    "3. В первой трети дня: внимание",
    "3. В последней трети дня: внимание",
    "4. В первой трети дня: анализ",
    "4. В последней трети дня: анализ",
    "5. Были ли моменты, когда принимал привычную линию?",
    "6. Были ли конкретные сбои внимания?",
]


def game(stamp, day, hours="4", autopilot="один-два отдельных эпизода"):
    return [stamp, day, hours, "заметно не менялось",
            "большую часть времени", "почти всегда",
            "выборочно", "выборочно", autopilot, "нет"]


class Sheet:
    def __init__(self, name, rows):
        self.title = name
        self.rows = [list(row) for row in rows]

    def get_all_values(self):
        return [row[:] for row in self.rows]

    def append_row(self, row):
        self.rows.append(list(row))

    def update(self, range_name, values):
        import re
        index = int(re.search(r"\d+", range_name)[0])
        self.rows[index - 1][5:7] = values[0]


class Book:
    def __init__(self):
        self.tabs = {
            "Утро — ответы": Sheet("Утро — ответы", [
                ["Отметка времени", "Дата", "Сегодня"],
                ["21.10.2026 08:00:00", "21.10.2026", "игра"],
            ]),
            "После игры — ответы": Sheet("После игры — ответы", [
                HEAD,
                game("21.10.2026 23:00:00", "21.10.2026", "3"),
            ]),
        }

    def worksheet(self, title):
        if title not in self.tabs:
            raise gspread.exceptions.WorksheetNotFound(title)
        return self.tabs[title]

    def add_worksheet(self, title, rows, cols):
        self.tabs[title] = Sheet(title, [])
        return self.tabs[title]


class EveningBriefTests(unittest.TestCase):
    def test_actual_evening_form_yields_compact_grounded_report(self):
        day = date(2026, 10, 22)
        book = Book()
        book.worksheet("После игры — ответы").append_row(
            game("22.10.2026 23:08:14", "22.10.2026")
        )
        rows = monitor.normalize_records(
            book.worksheet("После игры — ответы").get_all_values(), TZ
        )
        text = evening.render(
            rows[-1], [], rows,
            plan_type=PlannedLoadCode("M", Decimal("5.5")),
        )
        self.assertIn("Постигровой отчёт", text)
        self.assertIn("🎲 Факт игры: 4 ч (заявлено после игры)", text)
        self.assertIn("📅 План нагрузки: M (5,5 ч Game OPT)", text)
        self.assertIn("Внимание (начало → конец): большую часть времени → почти всегда", text)
        self.assertIn("Автоматизм: один-два отдельных эпизода; конкретные сбои: нет", text)
        self.assertIn("3 дня: 2 анкет, 7 ч в 2 анкетах", text)
        self.assertIn("7 дней: 2 анкет", text)
        self.assertNotIn("доказан", text.lower())
        self.assertLess(len(text), 1350)
        self.assertIn("<b>Постигровой отчёт</b>", monitor.telegram_report_html(text))
        self.assertIn("<b>🎲 Факт игры:</b>", monitor.telegram_report_html(text))


    def test_historical_date_reader_uses_no_future_data_or_writes(self):
        book = Book()
        book.worksheet("После игры — ответы").append_row(
            game("22.10.2026 23:08:14", "22.10.2026", "4")
        )
        book.worksheet("После игры — ответы").append_row(
            game("23.10.2026 22:30:00", "23.10.2026", "6")
        )
        book.worksheet("Утро — ответы").append_row(
            ["23.10.2026 08:00:00", "23.10.2026", "игра"]
        )
        before = {name: tab.get_all_values() for name, tab in book.tabs.items()}
        record, mornings, games = evening.read_historical_inputs(
            book, date(2026, 10, 22)
        )
        self.assertEqual(record["date"], date(2026, 10, 22))
        self.assertEqual(record["fields"]["1. Сколько часов фактически играл сегодня?"], "4")
        self.assertTrue(all(row["date"] <= date(2026, 10, 22) for row in mornings))
        self.assertTrue(all(row["date"] <= date(2026, 10, 22) for row in games))
        self.assertEqual(len(games), 2)
        self.assertEqual(before, {name: tab.get_all_values() for name, tab in book.tabs.items()})

    def test_historical_date_reader_missing_evening_form(self):
        book = Book()
        self.assertIsNone(
            evening.read_historical_inputs(book, date(2026, 10, 22))
        )

    def test_historical_date_reader_rejects_invalid_date(self):
        book = Book()
        with self.assertRaises(ValueError):
            evening.read_historical_inputs(book, "22.10.2026")

    def test_no_game_hours_does_not_invent_duration(self):
        book = Book()
        r = game("22.10.2026 23:08:14", "22.10.2026", "")
        book.worksheet("После игры — ответы").append_row(r)
        rows = monitor.normalize_records(
            book.worksheet("После игры — ответы").get_all_values(), TZ
        )
        text = evening.render(rows[-1], [], rows)
        self.assertIn("время игры не указано", text)

    def test_ai_action_cannot_automatically_cut_load(self):
        book = Book()
        for day in ("20", "22"):
            book.worksheet("После игры — ответы").append_row(
                game(f"{day}.10.2026 22:00:00", f"{day}.10.2026")
            )
        rows = monitor.normalize_records(
            book.worksheet("После игры — ответы").get_all_values(), TZ
        )
        text = evening.render(rows[-1], [], rows, insight={
            "hypothesis": "Повторный автоматизм может усиливаться к концу сессии.",
            "curator_action": "Снизить нагрузку автоматически на 50%.",
        })
        self.assertNotIn("Снизить нагрузку", text)


class EveningMonitorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = patch.dict(os.environ, {
            "ZHUKOV_MONITOR_ENABLED": "true",
            "TELEGRAM_USER_ID": "900",
            "ZHUKOV_CURATOR_CHAT_ID": "900",
            "ZHUKOV_TELEGRAM_USER_ID": "901",
            "ZHUKOV_SHEET_TIMEZONE": "Asia/Shanghai",
            "ZHUKOV_MONITOR_EVENT_MODE": "script",
            "ZHUKOV_MONITOR_ANCHOR_LOCAL": "2026-10-20T08:00:00",
            "ZHUKOV_MONITOR_ANCHOR_REPORT_DATE": "2026-10-20",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.book = Book()
        self.book.tabs["Everest_Алексей_события"] = Sheet(
            "Everest_Алексей_события", [
                ["EVENT_KEY", "SOURCE_TAB", "REPORT_DATE"],
                ["После игры — ответы:2", "После игры — ответы", "21.10.2026"],
            ]
        )
        class Bot:
            messages = None
            def __init__(self):
                self.messages = []
            async def send_message(self, chat_id, text, **kwargs):
                self.messages.append((chat_id, text))
        self.bot = Bot()

    async def test_no_historical_postgame_flood_then_one_new_report(self):
        first = datetime(2026, 10, 22, 18, tzinfo=TZ)
        await monitor.tick(self.bot, self.book, first, plan_types={})
        audit = self.book.worksheet(monitor.LOG_TAB).get_all_values()
        self.assertTrue(any(r[0] == "zhukov:evening-monitor:activation" for r in audit))
        self.assertFalse(any(r[1] == "EVENING_ANALYSIS" for r in audit if len(r) > 1))
        self.book.worksheet("После игры — ответы").append_row(
            game("22.10.2026 23:08:14", "22.10.2026")
        )
        # Without the Apps Script event, do not send a report.
        await monitor.tick(self.bot, self.book, datetime(2026, 10, 22, 23, 12, tzinfo=TZ),
                           plan_types={})
        self.assertFalse(any(r[1] == "EVENING_ANALYSIS" for r in
                             self.book.worksheet(monitor.LOG_TAB).get_all_values()
                             if len(r) > 1))
        self.book.worksheet("Everest_Алексей_события").append_row(
            ["После игры — ответы:3", "После игры — ответы", "22.10.2026"]
        )
        with patch("zhukov_evening.enrich_report", return_value="EVENING VERIFIED"):
            await monitor.tick(self.bot, self.book, datetime(2026, 10, 22, 23, 15, tzinfo=TZ),
                               plan_types={})
            await monitor.tick(self.bot, self.book, datetime(2026, 10, 22, 23, 20, tzinfo=TZ),
                               plan_types={})
        audit = self.book.worksheet(monitor.LOG_TAB).get_all_values()
        delivered = [r for r in audit if len(r) > 1 and r[1] == "EVENING_ANALYSIS"]
        self.assertEqual(len(delivered), 1)
        self.assertEqual(delivered[0][2], "2026-10-22")
        self.assertEqual(delivered[0][5], "SENT")
        self.assertEqual(sum("EVENING VERIFIED" in text for _, text in self.bot.messages), 1)
        self.assertTrue(all(chat == 900 for chat, text in self.bot.messages
                            if "EVENING VERIFIED" in text))

    async def test_evening_optional_ai_without_key_delivers_fallback(self):
        self.book.worksheet("После игры — ответы").append_row(
            game("22.10.2026 23:08:14", "22.10.2026")
        )
        games = monitor.normalize_records(
            self.book.worksheet("После игры — ответы").get_all_values(), TZ
        )
        with patch("zhukov_ai.is_enabled", return_value=False), patch(
                "zhukov_evening.aiohttp.ClientSession") as api:
            result = await evening.enrich_report(games[-1], [], games)
        self.assertIn("Постигровой отчёт", result)
        self.assertIn("AI-анализ недоступен", result)
        api.assert_not_called()


if __name__ == "__main__":
    unittest.main()
