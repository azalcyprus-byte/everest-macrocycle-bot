"""New standalone evening-form monitoring; no legacy schema assumptions."""
import os
import unittest
from datetime import date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import gspread
import zhukov_evening_v2 as v2
import zhukov_monitor as monitor

NEW_HEADERS = [
    "Timestamp",
    "Дата",
    "Чем ты занимался сегодня? (1 или несколько вариантов)",
    "Сколько минут физической активности было сегодня?",
    "Сколько минут сегодня смотрел видеоразборы?",
    "Сколько минут сегодня занимался активным анализом трансляций?",
    "Насколько умственно тяжёлым было сегодняшнее обучение?",
    "Что ощущалось в теле к концу дня?",
    "Что сегодня могло повлиять на твоё состояние?",
    "Сегодня был игровой день?",
    "1. Сколько часов фактически играл сегодня?",
    "2. Менялось ли твоё состояние по ходу игры?",
    "3. В первой трети дня: когда сам не участвовал в раздаче, насколько часто следил за происходящим?",
    "4. Были ли моменты, когда принимал первую привычную линию, не рассматривая нормально другие варианты?",
    "5. Были ли конкретные сбои внимания?",
    "Короткий пример сбоя внимания, если был",
    "6. Если бы сейчас нужно было играть ещё два часа: ( Отвечай только после игровых сессий продолжительностью 8 часов и более)",
]
MORNING_HEADERS = [
    "Отметка времени", "Дата",
    "1. Во сколько лег спать?", "2. Во сколько окончательно проснулся?",
    "3. Сколько раз просыпался за ночь?", "4. Как быстро уснул?",
    "6. Через 30–60 минут после пробуждения сонливость осталась?",
    "8. Когда читаешь сообщения или обычный текст, приходится перечитывать, потому что внимание уходит?",
]


def evening(stamp, day, is_game="Нет", hours="", lapses="Нет"):
    return [
        stamp, day, "Обучение", "45", "25", "50", "средне",
        "небольшая усталость", "сон", is_game, hours,
        "состояние стабильное", "почти всегда", "Нет", lapses,
        "не было", "",
    ]


class Sheet:
    def __init__(self, title, rows):
        self.title = title
        self.rows = [list(x) for x in rows]
    def get_all_values(self):
        return [r[:] for r in self.rows]
    def append_row(self, row):
        self.rows.append(list(row))
    def update(self, range_name, values):
        import re
        num = int(re.search(r"\d+", range_name).group())
        self.rows[num - 1][5:7] = values[0]


class Book:
    def __init__(self, is_new):
        self.tabs = (
            {v2.NEW_FORM_TAB: Sheet(v2.NEW_FORM_TAB, [NEW_HEADERS])}
            if is_new else
            {"Утро — ответы": Sheet("Утро — ответы", [
                MORNING_HEADERS, [
                    "21.11.2026 08:10:00", "21.11.2026", "23:40", "08:00",
                    "1", "До 15 минут", "нет", "нет",
                ],
            ])}
        )
    def worksheet(self, title):
        if title not in self.tabs:
            raise gspread.exceptions.WorksheetNotFound(title)
        return self.tabs[title]
    def add_worksheet(self, title, rows, cols):
        assert title not in self.tabs
        self.tabs[title] = Sheet(title, [])
        return self.tabs[title]


class FakeBot:
    def __init__(self):
        self.sent = []
    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))


class NewEveningTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            "ZHUKOV_EVENING_SHEET_TIMEZONE": "Etc/GMT-3",
            "ZHUKOV_SHEET_TIMEZONE": "Asia/Shanghai",
            "ZHUKOV_MONITOR_ENABLED": "true",
            "TELEGRAM_USER_ID": "900",
            "ZHUKOV_CURATOR_CHAT_ID": "900",
            "ZHUKOV_TELEGRAM_USER_ID": "901",
            "ZHUKOV_EVENING_RESPONSES_TAB": v2.NEW_FORM_TAB,
        })
        self.env.start()
        self.addCleanup(self.env.stop)
    def test_exact_new_headers_and_english_timestamp(self):
        sheet = Book(True).worksheet(v2.NEW_FORM_TAB)
        sheet.append_row(evening("11/21/2026 6:15:20 PM", "21.11.2026"))
        sheet.append_row(evening("11/21/2026 19:00:00", "2026-11-21", "Да", "4"))
        records = v2.parse_records(sheet.get_all_values())
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["date"], date(2026, 11, 21))
        self.assertEqual(records[0]["timestamp"].hour, 19)
        self.assertEqual(records[0]["timestamp"].utcoffset(), timedelta(hours=3))
    def test_no_guessing_missing_report_date_or_schema(self):
        sheet = Book(True).worksheet(v2.NEW_FORM_TAB)
        sheet.append_row(evening("11/21/2026 6:15:20 PM", ""))
        self.assertEqual(v2.parse_records(sheet.get_all_values()), [])
        with self.assertRaises(ValueError):
            v2.parse_records([["Timestamp", "Дата"]])
    def test_training_day_has_all_day_activity_and_sleep_not_fake_game(self):
        new, old = Book(True), Book(False)
        new.worksheet(v2.NEW_FORM_TAB).append_row(
            evening("11/21/2026 6:15:20 PM", "21.11.2026", "Нет"))
        source = v2.historical_inputs(new, old, date(2026, 11, 21))
        self.assertIsNotNone(source)
        report = v2.render(*source)
        self.assertIn("неигровой день", report)
        self.assertIn("активный анализ трансляций: 50 мин", report)
        self.assertIn("Засыпание: До 15 минут", report)
        self.assertIn("не фактический сон", report)
        self.assertNotIn("🎮 Во время игры", report)
        self.assertIn("Тело к концу дня:", report)
    def test_game_day_uses_only_new_form_game_fields(self):
        source = Book(True)
        source.worksheet(v2.NEW_FORM_TAB).append_row(
            evening("11/21/2026 6:15:20 PM", "21.11.2026", "Да", "9", "Были"))
        rows = v2.parse_records(source.worksheet(v2.NEW_FORM_TAB).get_all_values())
        report = v2.render(rows[0], [], rows)
        self.assertIn("9,0 ч", report)
        self.assertIn("Сигналы для проверки: сбои внимания", report)
        self.assertIn("🎮 Во время игры", report)
        self.assertIn("Утренней анкеты за эту дату нет", report)
    def test_manual_history_never_sees_future_day(self):
        new, old = Book(True), Book(False)
        new.worksheet(v2.NEW_FORM_TAB).append_row(
            evening("11/21/2026 6:15:20 PM", "21.11.2026"))
        new.worksheet(v2.NEW_FORM_TAB).append_row(
            evening("11/22/2026 6:15:20 PM", "22.11.2026"))
        rec, mornings, evenings = v2.historical_inputs(
            new, old, date(2026, 11, 21))
        self.assertEqual(rec["date"], date(2026, 11, 21))
        self.assertEqual(len(evenings), 1)


class NewEveningDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = patch.dict(os.environ, {
            "ZHUKOV_EVENING_SHEET_TIMEZONE": "Etc/GMT-3",
            "ZHUKOV_SHEET_TIMEZONE": "Asia/Shanghai",
            "ZHUKOV_MONITOR_ENABLED": "true",
            "TELEGRAM_USER_ID": "900",
            "ZHUKOV_CURATOR_CHAT_ID": "900",
            "ZHUKOV_TELEGRAM_USER_ID": "901",
            "ZHUKOV_EVENING_RESPONSES_TAB": v2.NEW_FORM_TAB,
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.new = Book(True)
        self.main = Book(False)
        self.bot = FakeBot()
    async def test_activation_then_one_new_response_sent_and_archived(self):
        now = datetime(2026, 11, 21, 15, tzinfo=ZoneInfo("Etc/GMT-3"))
        await v2.tick(self.bot, self.main, self.new,
                      now=now, plan_types={})
        self.assertEqual(len(self.bot.sent), 0)
        self.new.worksheet(v2.NEW_FORM_TAB).append_row(
            evening("11/21/2026 6:30:00 PM", "21.11.2026", "Нет"))
        await v2.tick(self.bot, self.main, self.new,
                      now=now + timedelta(hours=4), plan_types={})
        await v2.tick(self.bot, self.main, self.new,
                      now=now + timedelta(hours=5), plan_types={})
        audit = self.main.worksheet(monitor.LOG_TAB).get_all_values()
        sent = [r for r in audit if len(r) > 1 and r[1] == "EVENING_ANALYSIS"]
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][5], "SENT")
        self.assertEqual(len(self.bot.sent), 1)
        self.assertEqual(self.bot.sent[0][0], 900)
        self.assertIn("Вечерний отчёт (новая Google Форма)", self.bot.sent[0][1])
    async def test_new_source_ignores_historical_rows_before_activation(self):
        self.new.worksheet(v2.NEW_FORM_TAB).append_row(
            evening("11/20/2026 6:30:00 PM", "20.11.2026", "Да", "5"))
        when = datetime(2026, 11, 21, 15, tzinfo=ZoneInfo("Etc/GMT-3"))
        await v2.tick(self.bot, self.main, self.new, now=when, plan_types={})
        await v2.tick(self.bot, self.main, self.new,
                      now=when + timedelta(days=1), plan_types={})
        self.assertEqual(self.bot.sent, [])
    async def test_no_source_changes_on_invalid_schema(self):
        self.new.worksheet(v2.NEW_FORM_TAB).rows[0] = ["Date", "Timestamp"]
        with self.assertRaises(ValueError):
            await v2.tick(
                self.bot, self.main, self.new,
                now=datetime(2026, 11, 21, 15, tzinfo=ZoneInfo("Etc/GMT-3")),
                plan_types={},
            )
        self.assertFalse(self.bot.sent)
        self.assertFalse(self.main.tabs.get(monitor.LOG_TAB))


if __name__ == "__main__":
    unittest.main()
