import os
import unittest
from datetime import datetime, date
from unittest.mock import patch
from zoneinfo import ZoneInfo

import gspread
import zhukov_monitor as m


TZ = ZoneInfo("Europe/Moscow")
HEAD = [
    "Отметка времени", "Дата", "Город", "Сегодня",
    "1. Во сколько лег спать?", "2. Во сколько окончательно проснулся?",
    "5. Как встал?",
    "6. Через 30–60 минут после пробуждения сонливость осталась?",
    "7. Что сейчас ощущается в теле?",
    "8. Когда читаешь сообщения или обычный текст, приходится перечитывать, потому что внимание уходит?",
    "9. Простые утренние дела и решения даются:",
]


def morning(stamp, day, hours=("23:00", "09:00"), sleepiness="немного"):
    return [stamp, day, "Москва", "игра", *hours, "обычно",
            sleepiness, "ничего необычного", "нет", "как обычно"]


class Sheet:
    def __init__(self, name, values=None):
        self.title = name
        self.rows = list(values or [])

    def get_all_values(self):
        return [list(r) for r in self.rows]

    def append_row(self, row):
        self.rows.append(list(row))

    def update(self, range_name, values):
        import re
        row_number = int(re.search(r"\d+", range_name).group())
        self.rows[row_number - 1][5:7] = values[0]


class Spreadsheet:
    def __init__(self, morning_rows, post_rows=None):
        self.tabs = {
            "Утро — ответы": Sheet("Утро — ответы", morning_rows),
            "После игры — ответы": Sheet("После игры — ответы", post_rows or [["Отметка времени", "Дата"]]),
        }

    def worksheet(self, title):
        if title not in self.tabs:
            raise gspread.exceptions.WorksheetNotFound(title)
        return self.tabs[title]

    def add_worksheet(self, title, rows, cols):
        self.tabs[title] = Sheet(title)
        return self.tabs[title]


class Bot:
    def __init__(self):
        self.messages = []

    async def send_message(self, chat_id, text):
        self.messages.append((chat_id, text))


class PureLogicTests(unittest.TestCase):
    def test_fixed_30h_then_24h(self):
        anchor = datetime(2026, 10, 1, 8, 0, tzinfo=TZ)
        checks = m.due_checks(anchor, datetime(2026, 10, 3, 14, 1, tzinfo=TZ),
                              date(2026, 10, 1))
        self.assertEqual(
            [(d.isoformat(), t.strftime("%H:%M")) for d, t in checks],
            [("2026-10-02", "14:00"), ("2026-10-03", "14:00")]
        )

    def test_evening_anchor_30h_falls_two_calendar_dates_later(self):
        anchor = datetime(2026, 10, 1, 23, 0, tzinfo=TZ)
        self.assertEqual(
            m.due_checks(anchor, datetime(2026, 10, 2, 23, 30, tzinfo=TZ), date(2026, 10, 1)),
            [],
        )
        due = m.due_checks(anchor, datetime(2026, 10, 3, 5, 0, tzinfo=TZ), date(2026, 10, 1))
        self.assertEqual([(d.isoformat(), t.strftime("%H:%M")) for d, t in due],
                         [("2026-10-02", "05:00")])

    def test_sheet_timestamp_requires_no_trainee_timezone(self):
        with patch.dict(os.environ, {
            "ZHUKOV_TIMEZONE": "Pacific/Auckland",
            "ZHUKOV_SHEET_TIMEZONE": "Asia/Shanghai",
            "ZHUKOV_CURATOR_CHAT_ID": "900",
        }):
            monitor_tz, sheet_tz, chat_id = m.config()
        self.assertEqual(monitor_tz.key, "Asia/Shanghai")
        self.assertEqual(sheet_tz.key, "Asia/Shanghai")
        self.assertEqual(chat_id, 900)
        records = m.normalize_records(
            [HEAD, morning("01.10.2026 08:00:00", "01.10.2026")], sheet_tz
        )
        chosen = m.anchor_local(records, sheet_tz, date(2026, 10, 1))
        self.assertEqual(chosen[1], date(2026, 10, 1))
        deadlines = m.due_checks(
            chosen[0], datetime(2026, 10, 2, 14, 0, tzinfo=sheet_tz),
            date(2026, 10, 1), chosen[1],
        )
        self.assertEqual(deadlines[0][0], date(2026, 10, 2))
        self.assertEqual(deadlines[0][1].strftime("%H:%M"), "14:00")

    def test_elapsed_24h_across_daylight_saving(self):
        # US spring-forward changes the wall clock, not the 24h elapsed cadence.
        ny = ZoneInfo("America/New_York")
        anchor = datetime(2026, 3, 7, 8, 0, tzinfo=ny)
        now = datetime(2026, 3, 9, 15, 0, tzinfo=ny)
        deadlines = m.due_checks(anchor, now, date(2026, 3, 7))
        self.assertEqual(
            [(day.isoformat(), stamp.strftime("%H:%M")) for day, stamp in deadlines],
            [("2026-03-08", "15:00"), ("2026-03-09", "15:00")],
        )
        self.assertEqual(
            (deadlines[1][1].timestamp() - deadlines[0][1].timestamp()) / 3600,
            24.0,
        )

    def test_exact_control_boundary(self):
        anchor = datetime(2026, 10, 1, 8, tzinfo=TZ)
        self.assertEqual(m.due_checks(anchor, datetime(2026, 10, 2, 13, 59, tzinfo=TZ),
                                      date(2026, 10, 1)), [])

    def test_late_record_uses_report_date(self):
        rows = [HEAD,
                morning("02.10.2026 18:00:00", "02.10.2026"),
                morning("02.10.2026 17:00:00", "02.10.2026")]
        records = m.normalize_records(rows, TZ)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["date"], date(2026, 10, 2))
        self.assertEqual(records[0]["timestamp"].hour, 18)

    def test_long_sleep_not_proof_of_fatigue(self):
        rec = m.normalize_records([HEAD, morning("01.10.2026 08:00:00",
                                                 "01.10.2026")], TZ)[0]
        self.assertEqual(m.sleep_window(rec), 10.0)
        out = m.report_text(rec, [rec], [])
        self.assertIn("не равняется фактическому сну", out)
        self.assertIn("Нет оснований автоматически снижать нагрузку", out)

    def test_separate_game_quality_indicators(self):
        header = ["Отметка времени", "Дата",
                  "3. В первой трети дня: внимание",
                  "3. В последней трети дня: внимание",
                  "4. В первой трети дня: анализ",
                  "4. В последней трети дня: анализ",
                  "5. Были ли моменты автоматизма",
                  "6. Были ли конкретные сбои внимания?"]
        row = ["02.10.2026 22:00:00", "02.10.2026", "почти всегда",
               "время от времени", "регулярно", "редко",
               "повторялось несколько раз", "несколько эпизодов"]
        rec = m.normalize_records([header, row], TZ)[0]
        self.assertEqual(m.quality_drop(rec), [True, True])
        self.assertTrue(m.automation(rec))
        self.assertTrue(m.concrete_lapse(rec))


class MonitoringTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        cfg = {
            "ZHUKOV_MONITOR_ENABLED": "true",
            "ZHUKOV_CURATOR_CHAT_ID": "900",
            "TELEGRAM_USER_ID": "900",
            "ZHUKOV_TELEGRAM_USER_ID": "901",
            "ZHUKOV_TIMEZONE": "Europe/Moscow",
            "ZHUKOV_SHEET_TIMEZONE": "Europe/Moscow",
            "ZHUKOV_MONITOR_EVENT_MODE": "poll",
            "ZHUKOV_MONITOR_ANCHOR_LOCAL": "2026-10-01T08:00:00",
        }
        self.env = patch.dict(os.environ, cfg)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.book = Spreadsheet([HEAD, morning("01.10.2026 08:00:00", "01.10.2026")])
        self.bot = Bot()

    async def test_first_run_only_current_missing_no_flood(self):
        await m.tick(self.bot, self.book, datetime(2026, 10, 20, 15, tzinfo=TZ))
        self.assertEqual(len(self.bot.messages), 1)
        self.assertIn("20.10.2026", self.bot.messages[0][1])
        self.assertEqual(self.bot.messages[0][0], 900)
        self.assertNotIn("август", self.bot.messages[0][1].lower())

    async def test_deduplicates_and_late_submission(self):
        await m.tick(self.bot, self.book, datetime(2026, 10, 2, 15, tzinfo=TZ))
        await m.tick(self.bot, self.book, datetime(2026, 10, 2, 16, tzinfo=TZ))
        self.assertEqual(len(self.bot.messages), 1)
        self.book.worksheet("Утро — ответы").append_row(
            morning("02.10.2026 18:00:00", "02.10.2026"))
        await m.tick(self.bot, self.book, datetime(2026, 10, 2, 19, tzinfo=TZ))
        await m.tick(self.bot, self.book, datetime(2026, 10, 2, 20, tzinfo=TZ))
        self.assertEqual(len(self.bot.messages), 2)
        self.assertEqual(sum("На контрольный момент" in x[1] for x in self.bot.messages), 1)
        self.assertTrue(all(chat == 900 for chat, _ in self.bot.messages))

    async def test_no_historical_anchor_before_first_new_report(self):
        os.environ.pop("ZHUKOV_MONITOR_ANCHOR_LOCAL", None)
        # Old history exists but must not set the new monitoring clock.
        await m.tick(self.bot, self.book, datetime(2026, 10, 20, 16, tzinfo=TZ))
        self.assertEqual(self.bot.messages, [])
        rows = self.book.worksheet(m.LOG_TAB).get_all_values()
        self.assertFalse(any(row[0] == "zhukov:monitor:anchor" for row in rows[1:]))
        # First post-activation report, 20 Oct at 08:00, becomes the anchor.
        self.book.worksheet("Утро — ответы").append_row(
            morning("20.10.2026 08:00:00", "20.10.2026"))
        await m.tick(self.bot, self.book, datetime(2026, 10, 20, 16, 5, tzinfo=TZ))
        self.assertEqual(len(self.bot.messages), 1)  # morning analysis
        self.assertTrue(any(row[0] == "zhukov:monitor:anchor"
                            for row in self.book.worksheet(m.LOG_TAB).get_all_values()[1:]))
        await m.tick(self.bot, self.book, datetime(2026, 10, 21, 13, 59, tzinfo=TZ))
        self.assertEqual(len(self.bot.messages), 1)
        await m.tick(self.bot, self.book, datetime(2026, 10, 21, 14, tzinfo=TZ))
        self.assertEqual(len(self.bot.messages), 2)
        self.assertIn("21.10.2026", self.bot.messages[-1][1])

    async def test_late_report_cannot_erase_missed_deadline(self):
        self.book.worksheet("Утро — ответы").append_row(
            morning("02.10.2026 14:30:00", "02.10.2026")
        )
        await m.tick(self.bot, self.book, datetime(2026, 10, 2, 15, tzinfo=TZ))
        messages = [text for _, text in self.bot.messages]
        self.assertEqual(sum("На контрольный момент" in x for x in messages), 1)
        self.assertEqual(sum("ФАКТ" in x for x in messages), 1)
        await m.tick(self.bot, self.book, datetime(2026, 10, 2, 17, tzinfo=TZ))
        self.assertEqual(len(self.bot.messages), 2)

    async def test_timely_report_does_not_trigger_missed_alert(self):
        self.book.worksheet("Утро — ответы").append_row(
            morning("02.10.2026 13:59:00", "02.10.2026")
        )
        await m.tick(self.bot, self.book, datetime(2026, 10, 2, 15, tzinfo=TZ))
        self.assertEqual(len(self.bot.messages), 1)
        self.assertIn("ФАКТ", self.bot.messages[0][1])

    async def test_script_mode_requires_event(self):
        os.environ["ZHUKOV_MONITOR_EVENT_MODE"] = "script"
        self.book.worksheet("Утро — ответы").append_row(
            morning("02.10.2026 13:00:00", "02.10.2026"))
        await m.tick(self.bot, self.book, datetime(2026, 10, 2, 15, tzinfo=TZ))
        self.assertEqual(len(self.bot.messages), 0)
        self.book.tabs["Everest_Алексей_события"] = Sheet(
            "Everest_Алексей_события",
            [["EVENT_KEY", "SOURCE_TAB", "REPORT_DATE"],
             ["Утро — ответы:3", "Утро — ответы", "02.10.2026"]])
        await m.tick(self.bot, self.book, datetime(2026, 10, 2, 15, tzinfo=TZ))
        self.assertEqual(len(self.bot.messages), 1)
        self.assertIn("НАБЛЮДЕНИЕ", self.bot.messages[0][1])


if __name__ == "__main__":
    unittest.main()
