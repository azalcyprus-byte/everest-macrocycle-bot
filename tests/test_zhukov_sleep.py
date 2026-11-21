"""Regression checks for Alexey's private Telegram menu and bedtime markers."""
import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import gspread

with patch.dict(os.environ, {
    "BOT_TOKEN": "123456789:FAKE_TEST_TOKEN_NOT_REAL",
    "TELEGRAM_USER_ID": "900",
    "ZHUKOV_TELEGRAM_USER_ID": "901",
    "ZHUKOV_TIMEZONE": "Asia/Phnom_Penh",
}):
    import bot

import zhukov_sleep


class FakeSheet:
    def __init__(self):
        self.rows = []

    def get_all_values(self):
        return [list(r) for r in self.rows]

    def append_row(self, row):
        self.rows.append(list(row))


class FakeBook:
    def __init__(self):
        self.tabs = {}

    def worksheet(self, title):
        if title not in self.tabs:
            raise gspread.exceptions.WorksheetNotFound(title)
        return self.tabs[title]

    def add_worksheet(self, title, rows, cols):
        assert title not in self.tabs
        assert cols == len(zhukov_sleep.HEADERS)
        tab = FakeSheet()
        self.tabs[title] = tab
        return tab


class BrokenBook:
    def worksheet(self, title):
        raise RuntimeError("Google Sheets offline")


class BedtimeStorageTests(unittest.TestCase):
    def test_exact_telegram_instant_and_cross_midnight_timezone(self):
        book = FakeBook()
        stamp = datetime(2026, 11, 21, 20, 50, tzinfo=timezone.utc)
        saved, local = zhukov_sleep.save_bedtime(
            book, stamp, local_timezone="Asia/Phnom_Penh"
        )
        self.assertTrue(saved)
        self.assertEqual(local.date().isoformat(), "2026-11-22")
        self.assertEqual(local.strftime("%H:%M:%S"), "03:50:00")
        rows = book.tabs[zhukov_sleep.TAB].get_all_values()
        self.assertEqual(tuple(rows[0]), zhukov_sleep.HEADERS)
        self.assertEqual(rows[1][1], "BEDTIME_BUTTON")
        self.assertEqual(rows[1][2:4], ["2026-11-22", "03:50:00"])
        self.assertEqual(rows[1][4], "2026-11-21T20:50:00+00:00")
        self.assertEqual(rows[1][5], "Asia/Phnom_Penh")
        self.assertEqual(rows[1][6], "telegram:bedtime_button")
        self.assertEqual(len(rows), 2)

    def test_repeat_within_30_minutes_is_not_written_even_after_restart(self):
        book = FakeBook()
        stamp = datetime(2026, 11, 21, 20, 0, tzinfo=timezone.utc)
        created, _ = zhukov_sleep.save_bedtime(book, stamp)
        again, first = zhukov_sleep.save_bedtime(
            book, stamp + timedelta(minutes=29)
        )
        later, _ = zhukov_sleep.save_bedtime(
            book, stamp + timedelta(minutes=31)
        )
        self.assertTrue(created)
        self.assertFalse(again)
        self.assertEqual(first, stamp.astimezone(ZoneInfo("Asia/Phnom_Penh")))
        self.assertTrue(later)
        self.assertEqual(len(book.tabs[zhukov_sleep.TAB].rows), 3)

    def test_reject_naive_timestamp_without_changing_google_sheets(self):
        book = FakeBook()
        with self.assertRaises(ValueError):
            zhukov_sleep.save_bedtime(book, datetime(2026, 11, 21, 20))
        self.assertEqual(book.tabs, {})

    def test_existing_unexpected_headers_fail_closed(self):
        book = FakeBook()
        book.tabs[zhukov_sleep.TAB] = FakeSheet()
        book.tabs[zhukov_sleep.TAB].rows = [["Wrong", "columns"]]
        with self.assertRaises(ValueError):
            zhukov_sleep.save_bedtime(
                book, datetime(2026, 11, 21, 20, tzinfo=timezone.utc)
            )
        self.assertEqual(book.tabs[zhukov_sleep.TAB].rows, [["Wrong", "columns"]])


class FakeMessage:
    def __init__(self, text, stamped):
        self.text = text
        self.date = stamped
        self.replies = []

    async def reply_text(self, message, **kwargs):
        self.replies.append((message, kwargs))


class FakeUpdate:
    def __init__(self, text, *, user_id=901, chat_type="private", when=None):
        self.effective_message = FakeMessage(
            text, when or datetime(2026, 11, 21, 20, tzinfo=timezone.utc)
        )
        self.effective_user = SimpleNamespace(id=user_id)
        self.effective_chat = SimpleNamespace(type=chat_type)


class BedtimeMenuTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.book = FakeBook()
        self.user = patch.object(bot, "ZHUKOV_TELEGRAM_USER_ID", 901)
        self.owner = patch.object(bot, "ALLOWED_USER_ID", 900)
        self.zone = patch.object(bot, "ZHUKOV_TZ", ZoneInfo("Asia/Phnom_Penh"))
        self.book_connection = patch.object(bot, "get_zhukov_spreadsheet",
                                            return_value=self.book)
        self.user.start()
        self.owner.start()
        self.zone.start()
        self.book_connection.start()
        self.addCleanup(self.user.stop)
        self.addCleanup(self.owner.stop)
        self.addCleanup(self.zone.stop)
        self.addCleanup(self.book_connection.stop)

    async def test_start_shows_three_persistent_buttons_only_to_alexey(self):
        update = FakeUpdate("/start")
        await bot.start(update, SimpleNamespace())
        markup = update.effective_message.replies[-1][1]["reply_markup"]
        self.assertTrue(markup.is_persistent)
        labels = [[b.text for b in row] for row in markup.keyboard]
        self.assertEqual(labels, [
            [bot.ZHUKOV_MENU_MORNING, bot.ZHUKOV_MENU_EVENING],
            [bot.ZHUKOV_MENU_SLEEP],
        ])
        curator = FakeUpdate("/start", user_id=900)
        await bot.start(curator, SimpleNamespace())
        self.assertFalse(any(
            "reply_markup" in kwargs for _, kwargs
            in curator.effective_message.replies
        ))

    async def test_morning_and_evening_return_existing_form_urls(self):
        with patch.object(bot, "ZHUKOV_MORNING_FORM_URL",
                          "https://docs.google.com/forms/d/e/AMORNING/viewform"), \
             patch.object(bot, "ZHUKOV_POSTGAME_FORM_URL",
                          "https://docs.google.com/forms/d/e/AEVENING/viewform"):
            for text, fragment in [
                (bot.ZHUKOV_MENU_MORNING, "AMORNING/viewform"),
                (bot.ZHUKOV_MENU_EVENING, "AEVENING/viewform"),
            ]:
                update = FakeUpdate(text)
                await bot.handle_message(update, SimpleNamespace())
                self.assertEqual(len(update.effective_message.replies), 1)
                answer, kwargs = update.effective_message.replies[0]
                self.assertIn(fragment, answer)
                self.assertIn("reply_markup", kwargs)
        self.assertEqual(self.book.tabs, {})

    async def test_sleep_button_records_once_and_confirms_only_after_write(self):
        timestamp = datetime(2026, 11, 21, 20, 50, tzinfo=timezone.utc)
        first = FakeUpdate(bot.ZHUKOV_MENU_SLEEP, when=timestamp)
        await bot.handle_message(first, SimpleNamespace())
        self.assertIn("сохранена", first.effective_message.replies[0][0])
        self.assertIn("22.11.2026 в 03:50", first.effective_message.replies[0][0])
        second = FakeUpdate(
            bot.ZHUKOV_MENU_SLEEP, when=timestamp + timedelta(seconds=20)
        )
        await bot.handle_message(second, SimpleNamespace())
        self.assertIn("уже сохранена", second.effective_message.replies[0][0])
        self.assertEqual(len(self.book.tabs[zhukov_sleep.TAB].rows), 2)

    async def test_sleep_button_rejects_groups_and_non_trainee(self):
        for update in [
            FakeUpdate(bot.ZHUKOV_MENU_SLEEP, chat_type="group"),
            FakeUpdate(bot.ZHUKOV_MENU_SLEEP, user_id=900),
            FakeUpdate(bot.ZHUKOV_MENU_SLEEP, user_id=999),
        ]:
            self.assertFalse(await bot.handle_zhukov_menu(
                update, SimpleNamespace(), bot.ZHUKOV_MENU_SLEEP
            ))
        self.assertEqual(self.book.tabs, {})

    async def test_google_sheets_failure_does_not_claim_saved(self):
        with patch.object(bot, "get_zhukov_spreadsheet", return_value=BrokenBook()):
            update = FakeUpdate(bot.ZHUKOV_MENU_SLEEP)
            await bot.handle_message(update, SimpleNamespace())
        result = update.effective_message.replies[0][0]
        self.assertIn("Не удалось сохранить", result)
        self.assertNotIn("отметка уже сохранена", result.lower())
        self.assertEqual(self.book.tabs, {})


if __name__ == "__main__":
    unittest.main()
