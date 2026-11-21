"""Owner M5 forms must route before AI and never enter Alexey's menu."""
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

with patch.dict(os.environ, {
    "BOT_TOKEN": "123456789:FAKE_TEST_TOKEN_NOT_REAL",
    "TELEGRAM_USER_ID": "900",
    "ZHUKOV_TELEGRAM_USER_ID": "901",
}):
    import bot


def make_update(text, user_id=900, chat_type="private"):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=user_id, type=chat_type),
        effective_message=SimpleNamespace(text=text, reply_text=AsyncMock()),
    )


class OwnerGameMenuTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for name, value in (
            ("ALLOWED_USER_ID", 900),
            ("ZHUKOV_TELEGRAM_USER_ID", 901),
        ):
            patcher = patch.object(bot, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_start_installs_exactly_two_persistent_buttons(self):
        update = make_update("/start")
        await bot.start(update, SimpleNamespace())
        markup = update.effective_message.reply_text.call_args.kwargs["reply_markup"]
        self.assertTrue(markup.is_persistent)
        self.assertFalse(markup.one_time_keyboard)
        self.assertEqual(
            [[button.text for button in row] for row in markup.keyboard],
            [[bot.OWNER_MENU_INGAME], [bot.OWNER_MENU_POSTGAME]],
        )

    async def test_each_button_opens_its_own_published_form_before_ai(self):
        expected = (
            (bot.OWNER_MENU_INGAME,
             "https://docs.google.com/forms/d/e/1FAIpQLSeRejUXPdZu4Fl3JThIkKU-lxzIl7dfjt5eBc6TJc31utI6Mg/viewform"),
            (bot.OWNER_MENU_POSTGAME,
             "https://docs.google.com/forms/d/e/1FAIpQLScGTgxHLON1ZcWK1FN0a-R3I9io-iSmROZ5rPuCyU5spxlkJg/viewform"),
        )
        for label, url in expected:
            update = make_update(label)
            with patch.object(bot, "_answer_everest_curator", new_callable=AsyncMock) as ai, \
                 patch.object(bot, "_handle_evening_snooze_message", new_callable=AsyncMock) as snooze:
                await bot.handle_message(update, SimpleNamespace())
                ai.assert_not_awaited()
                snooze.assert_not_awaited()
            update.effective_message.reply_text.assert_awaited_once()
            call = update.effective_message.reply_text.call_args
            self.assertIn(url, call.args[0])
            buttons = call.kwargs["reply_markup"].inline_keyboard
            self.assertEqual(len(buttons), 1)
            self.assertEqual(buttons[0][0].url, url)

    async def test_menu_rejects_other_users_alexey_and_group_chats(self):
        for user_id, chat_type in ((902, "private"), (901, "private"), (900, "group")):
            update = make_update(bot.OWNER_MENU_INGAME, user_id, chat_type)
            self.assertFalse(await bot.handle_owner_game_menu(
                update, SimpleNamespace(), bot.OWNER_MENU_INGAME,
            ))
            update.effective_message.reply_text.assert_not_awaited()

    async def test_other_text_continues_normal_message_processing(self):
        update = make_update("Доброе утро")
        self.assertFalse(await bot.handle_owner_game_menu(
            update, SimpleNamespace(), update.effective_message.text,
        ))
        update.effective_message.reply_text.assert_not_awaited()

    async def test_forms_command_returns_both_owner_links_only_in_private(self):
        update = make_update("/forms")
        await bot.zhukov_forms(update, SimpleNamespace())
        markup = update.effective_message.reply_text.call_args.kwargs["reply_markup"]
        self.assertEqual(
            [row[0].url for row in markup.inline_keyboard],
            [bot.INGAME_STATE_FORM_URL, bot.POSTGAME_FORM_URL],
        )
        group = make_update("/forms", chat_type="group")
        await bot.zhukov_forms(group, SimpleNamespace())
        group.effective_message.reply_text.assert_not_awaited()

    async def test_startup_uses_stable_journal_key_and_owner_destination(self):
        with patch.object(bot, "_send_morning_message_once", new_callable=AsyncMock) as send:
            await bot.install_owner_game_menu(SimpleNamespace())
            await bot.install_owner_game_menu(SimpleNamespace())
        first, second = send.await_args_list
        self.assertEqual(first.kwargs["chat_id"], 900)
        self.assertEqual(first.kwargs["idempotency_key"], second.kwargs["idempotency_key"])
        self.assertEqual(first.kwargs["operation"], "OWNER_GAME_MENU")

    async def test_unconfigured_url_does_not_make_invalid_telegram_button(self):
        update = make_update(bot.OWNER_MENU_INGAME)
        with patch.object(bot, "INGAME_STATE_FORM_URL", ""):
            self.assertTrue(await bot.handle_owner_game_menu(
                update, SimpleNamespace(), bot.OWNER_MENU_INGAME,
            ))
        self.assertNotIn("reply_markup", update.effective_message.reply_text.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
