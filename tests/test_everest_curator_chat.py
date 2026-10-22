"""Tests for curator-only, read-only OpenAI chat using anonymized Sheets facts."""
import json
import os
import unittest
from datetime import date
from unittest.mock import patch

import everest_curator_chat as curator
import zhukov_plan


class FakeSheet:
    def __init__(self, values):
        self._values = values
        self.writes = 0

    def get_all_values(self):
        return self._values

    def append_row(self, row):
        self.writes += 1
        raise AssertionError("Read-only curator must not write")


class FakeBook:
    def __init__(self):
        self.tabs = {
            "Утро — ответы": FakeSheet([
                ["Отметка времени", "Дата", "Сегодня", "Город",
                 "6. Через 30–60 минут после пробуждения сонливость осталась?",
                 "7. Что сейчас ощущается в теле?",
                 "8. Когда читаешь сообщения или обычный текст, приходится перечитывать, потому что внимание уходит?",
                 "9. Простые утренние дела и решения даются:",
                 "Свободный комментарий"],
                ["20.10.2026 08:30:00", "20.10.2026", "перелёт",
                 "Пекин", "немного", "ничего необычного", "нет",
                 "как обычно", "Это очень личный секрет"],
            ]),
            "После игры — ответы": FakeSheet([
                ["Отметка времени", "Дата",
                 "1. Сколько часов фактически играл сегодня?"],
            ]),
        }

    def worksheet(self, title):
        return self.tabs[title]


class CuratorFactsTests(unittest.TestCase):
    def test_source_is_anonymous_and_dated_by_offset_not_real_dates(self):
        plan = {
            date(2026, 10, 20): zhukov_plan.PlannedLoadCode("O", 0),
            date(2026, 10, 21): zhukov_plan.PlannedLoadCode("L", 3.5),
        }
        book = FakeBook()
        facts = curator.build_facts(
            book, plan, today=date(2026, 10, 20), days=7,
        )
        content = json.dumps(facts, ensure_ascii=False)
        self.assertNotIn("Пекин", content)
        self.assertNotIn("личный секрет", content)
        self.assertNotIn("Алексей", content)
        self.assertNotIn("Жуков", content)
        self.assertNotIn("20.10.2026", content)
        self.assertNotIn("2026-10-20", content)
        self.assertEqual(len(facts["timeline"]), 14)  # 7 back, 7 ahead
        curr = next(x for x in facts["timeline"] if x["days_from_today"] == 0)
        self.assertEqual(curr["plan_type"], "O")
        self.assertEqual(curr["planned_opt_game_hours"], 0.0)
        self.assertTrue(curr["morning_submitted"])
        self.assertFalse(curr["postgame_submitted"])
        self.assertEqual(curr["morning"]["day_type"], "перелёт")
        nxt = next(x for x in facts["timeline"] if x["days_from_today"] == 1)
        self.assertEqual(nxt["plan_type"], "L")
        self.assertEqual(nxt["planned_opt_game_hours"], 3.5)
        self.assertNotIn("morning", nxt)
        self.assertEqual(
            [tab.writes for tab in book.tabs.values()],
            [0, 0],
        )


    def test_checklist_submission_answer_reads_real_date_and_timestamp(self):
        book = FakeBook()
        book.tabs["После игры — ответы"] = FakeSheet([
            ["Отметка времени", "Дата", "1. Сколько часов фактически играл сегодня?"],
            ["22.10.2026 23:08:14", "22.10.2026", "4"],
        ])
        reply = curator.checklist_status_answer(
            "За 22 октября заполнен?", book, today=date(2026, 10, 23)
        )
        self.assertIn("22.10.2026", reply)
        self.assertIn("Постигровой отчёт: заполнен 22.10.2026 в 23:08", reply)
        self.assertNotIn("Постигровой отчёт: записи", reply)
        self.assertIn("Утренний отчёт: записи в Google Sheets нет", reply)

    def test_status_answer_no_invented_future_date_or_report(self):
        book = FakeBook()
        reply = curator.checklist_status_answer(
            "Вечерний отчёт за 22 октября заполнен?",
            book, today=date(2026, 10, 25)
        )
        self.assertIn("22.10.2026", reply)
        self.assertIn("Постигровой отчёт: записи в Google Sheets нет", reply)
        self.assertNotIn("Утренний отчёт", reply)
        self.assertIsNone(curator.checklist_status_answer(
            "Почему не заполнил отчёт?", book, today=date(2026, 10, 25)
        ))

    def test_only_exact_plan_days_and_explicit_missingness(self):
        facts = curator.build_facts(
            FakeBook(), {}, today=date(2026, 10, 20), days=7,
        )
        self.assertEqual(facts["morning_reports"], 1)
        self.assertEqual(facts["postgame_reports"], 0)
        tomorrow = next(x for x in facts["timeline"] if x["days_from_today"] == 1)
        self.assertIsNone(tomorrow["plan_type"])
        self.assertIsNone(tomorrow["planned_opt_game_hours"])

    def test_questions_are_bounded_and_strip_identifiers(self):
        raw = "Скажи про Алексея Жукова: mail@example.com https://abc.example"
        clean = curator._question(raw)
        self.assertNotIn("Алексея Жукова", clean)
        self.assertNotIn("mail@example.com", clean)
        self.assertNotIn("https://", clean)
        self.assertIn("игрок", clean)
        for bad in ("", "x" * 1601):
            with self.assertRaises(ValueError):
                curator._question(bad)

    def test_only_explicitly_configured_model_and_key_enable_chat(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "", "EVEREST_CURATOR_AI_ENABLED": "true"}):
            self.assertFalse(curator.enabled())
        with patch.dict(os.environ, {"OPENAI_API_KEY": "placeholder", "EVEREST_CURATOR_AI_ENABLED": "false"}):
            self.assertFalse(curator.enabled())
        with patch.dict(os.environ, {"OPENAI_API_KEY": "placeholder", "EVEREST_CURATOR_AI_ENABLED": "true"}):
            self.assertTrue(curator.enabled())


class CuratorAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        curator._HISTORY.clear()
        curator._LOCKS.clear()

    async def test_openai_request_is_read_only_bounded_and_costed(self):
        reply = {
            "status": "completed",
            "model": "gpt-6.1-sol",
            "usage": {"input_tokens": 900, "output_tokens": 100},
            "output": [{"content": [{"type": "output_text",
                                    "text": "За неделю нет подтверждённого тренда. Сохранить план."}]}],
        }
        class Response:
            status = 200
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                return False
            async def json(self):
                return reply

        class Session:
            payload = None
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                return False
            def post(self, url, **kwargs):
                self.url = url
                self.payload = kwargs["json"]
                return Response()

        fake = Session()
        with patch.dict(os.environ, {
            "OPENAI_API_KEY": "TEST_KEY_DO_NOT_USE",
            "EVEREST_CURATOR_AI_ENABLED": "true",
            "EVEREST_CURATOR_AI_MODEL": "gpt-6.1-sol",
        }), patch("everest_curator_chat.aiohttp.ClientSession", return_value=fake), patch(
            "everest_curator_chat.zhukov_plan.optional_plan_types", return_value={}
        ), patch("everest_curator_chat.build_facts", return_value={
            "history_days": 7, "timeline": [],
        }):
            result = await curator.answer_question(
                100001, "Проанализируй последние 7 дней", FakeBook()
            )
        self.assertIn("нет подтверждённого тренда", result)
        self.assertIn("💳 OpenAI API: ≈ $", result)
        self.assertFalse(fake.payload["store"])
        self.assertEqual(fake.payload["model"], "gpt-6.1-sol")
        self.assertEqual(fake.payload["reasoning"], {"effort": "medium"})
        self.assertEqual(fake.payload["max_output_tokens"], 2200)
        self.assertIn("≈ $0.002800", result)
        self.assertTrue(fake.payload["input"][-1]["content"].startswith("Проанализируй"))
        self.assertEqual(fake.url, "https://api.openai.com/v1/responses")
        self.assertNotIn("name", fake.payload["input"][0]["content"].lower())
        self.assertEqual(len(curator._HISTORY[100001]), 2)
        curator.reset_dialogue(100001)
        self.assertNotIn(100001, curator._HISTORY)


    async def test_factual_submission_question_skips_paid_api(self):
        book = FakeBook()
        book.tabs["После игры — ответы"] = FakeSheet([
            ["Отметка времени", "Дата"],
            ["22.10.2026 23:08:14", "22.10.2026"],
        ])
        with patch.dict(os.environ, {
            "OPENAI_API_KEY": "TEST_KEY_DO_NOT_USE",
            "EVEREST_CURATOR_AI_ENABLED": "true",
        }), patch("everest_curator_chat.aiohttp.ClientSession") as api, patch(
            "everest_curator_chat.build_facts"
        ) as facts:
            answer = await curator.answer_question(
                100004, "Вечерний отчёт за 22.10.2026 заполнен?", book
            )
        self.assertIn("✅ Постигровой отчёт: заполнен 22.10.2026 в 23:08", answer)
        api.assert_not_called()
        facts.assert_not_called()

    async def test_invalid_api_response_does_not_claim_success_or_store_history(self):
        class Response:
            status = 200
            async def __aenter__(self): return self
            async def __aexit__(self, *args): return False
            async def json(self): return {"status": "incomplete", "output": []}

        class Session:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): return False
            def post(self, url, **kwargs): return Response()

        with patch.dict(os.environ, {
            "OPENAI_API_KEY": "TEST_KEY_DO_NOT_USE",
            "EVEREST_CURATOR_AI_ENABLED": "true",
        }), patch("everest_curator_chat.aiohttp.ClientSession", return_value=Session()), patch(
            "everest_curator_chat.zhukov_plan.optional_plan_types", return_value={}
        ), patch("everest_curator_chat.build_facts", return_value={}):
            with self.assertRaisesRegex(RuntimeError, "Неполный"):
                await curator.answer_question(100002, "Как дела?", FakeBook())
        self.assertNotIn(100002, curator._HISTORY)

    async def test_missing_key_refuses_without_api_or_sheets(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}), patch(
            "everest_curator_chat.aiohttp.ClientSession"
        ) as session, patch(
            "everest_curator_chat.zhukov_plan.optional_plan_types"
        ) as plan:
            with self.assertRaisesRegex(RuntimeError, "недоступен"):
                await curator.answer_question(100003, "Анализ", FakeBook())
            session.assert_not_called()
            plan.assert_not_called()


if __name__ == "__main__":
    unittest.main()
