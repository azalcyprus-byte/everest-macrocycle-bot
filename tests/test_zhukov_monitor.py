import os
import unittest
from datetime import datetime, date
from unittest.mock import patch
from zoneinfo import ZoneInfo

import gspread
import json
import zhukov_ai as ai
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
        self.parse_modes = []

    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append((chat_id, text))
        self.parse_modes.append(kwargs.get("parse_mode"))

    async def send_photo(self, chat_id, photo, caption, **kwargs):
        self.messages.append((chat_id, caption))
        self.parse_modes.append(kwargs.get("parse_mode"))
        self.photo = photo.read()


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



class HistoricalReportsTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            "ZHUKOV_SHEET_TIMEZONE": "Europe/Moscow",
            "ZHUKOV_MACROCYCLE_PHASE": "prepeak",
            "ZHUKOV_PRIORITY_TOURNAMENTS": "Demo tournament",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.book = Spreadsheet([
            HEAD,
            morning("19.10.2026 07:00:00", "19.10.2026"),
            morning("20.10.2026 08:00:00", "20.10.2026"),
        ])

    def test_original_sent_report_is_replayed_verbatim(self):
        sheet = self.book.add_worksheet(m.LOG_TAB, rows=100, cols=8)
        sheet.append_row(m.HEADERS)
        original = "📊 Сохранённый отчёт: факты и рекомендации за 20 октября."
        sheet.append_row([
            "zhukov:analysis:2026-10-20", "ANALYSIS",
            "2026-10-20", "2026-10-20T08:00:00+03:00",
            original, "SENT", "", "",
        ])
        # A stored curator decision should be visible with the original report.
        sheet.append_row([
            "zhukov:decision:2026-10-20:abc", "CURATOR_DECISION",
            "2026-10-20", "2026-10-20T11:00:00+03:00",
            "Куратор: сохранить нагрузку", "CONFIRMED", "",
            "сохранить нагрузку",
        ])
        before = sheet.get_all_values()
        result = m.read_curator_report(self.book, date(2026, 10, 20))
        self.assertIn("💾 История Everest", result)
        self.assertIn(original, result)
        self.assertIn("сохранить нагрузку", result)
        self.assertEqual(sheet.get_all_values(), before)
        self.assertEqual(len(self.book.worksheet("Утро — ответы").rows), 3)

    def test_reconstructed_historical_report_does_not_use_current_phase(self):
        result = m.read_curator_report(self.book, date(2026, 10, 19))
        self.assertIn("Ретроспективная", result)
        self.assertIn("🏔️ Everest", result)
        self.assertIn("🔎 Значимые изменения", result)
        self.assertIn("💡 Гипотеза", result)
        self.assertIn("🎯 Рекомендация куратору", result)
        self.assertNotIn("ФАКТ\n", result)
        self.assertNotIn("Весь наблюдаемый период", result)
        self.assertNotIn("Demo tournament", result)
        self.assertNotIn("prepeak", result)
        self.assertTrue(m.read_curator_report(self.book).startswith("🔎 Архив Everest"))

    def test_history_index_and_date_selection(self):
        sheet = self.book.add_worksheet(m.LOG_TAB, rows=100, cols=8)
        sheet.append_row(m.HEADERS)
        sheet.append_row([
            "zhukov:missing:2026-10-21", "MISSING",
            "2026-10-21", "2026-10-21T14:00:00+03:00",
            "Пропущен утренний отчёт", "SENT", "", "",
        ])
        sheet.append_row([
            "zhukov:monitor:activation", "CONFIG",
            "2026-11-01", "", "", "SENT", "", "",
        ])
        listing = m.read_curator_history(self.book, limit=3)
        self.assertIn("21.10.2026 — ⚠️ пропуск срока", listing)
        self.assertIn("20.10.2026 — 🔎 утренняя анкета", listing)
        self.assertNotIn("01.11.2026", listing)
        missing = m.read_curator_report(self.book, date(2026, 10, 21))
        self.assertIn("зафиксирован пропуск", missing)
        empty = m.read_curator_report(self.book, date(2026, 10, 18))
        self.assertIn("Это не доказательство пропуска", empty)

    def test_only_postgame_report_still_available(self):
        self.book.worksheet("Утро — ответы").rows = [HEAD]
        self.book.worksheet("После игры — ответы").rows = [
            ["Отметка времени", "Дата", "1. Сколько часов фактически играл сегодня?"],
            ["20.10.2026 22:00:00", "20.10.2026", "6"],
        ]
        result = m.read_curator_report(self.book, date(2026, 10, 20))
        self.assertIn("Утреннего чек-листа за эту дату нет", result)
        self.assertIn("Фактическая игра: 6 ч", result)

    def test_pending_report_not_represented_as_delivered(self):
        sheet = self.book.add_worksheet(m.LOG_TAB, rows=100, cols=8)
        sheet.append_row(m.HEADERS)
        sheet.append_row([
            "zhukov:analysis:2026-10-20", "ANALYSIS",
            "2026-10-20", "", "UNDELIVERED CONTENT",
            "REVIEW", "Telegram error", "",
        ])
        result = m.read_curator_report(self.book, date(2026, 10, 20))
        self.assertIn("Ретроспективная", result)
        self.assertNotIn("UNDELIVERED CONTENT", result)

    def test_long_reports_are_not_truncated(self):
        report = "ФАКТ " * 2200
        parts = m.split_telegram_text(report)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(p) <= 3400 for p in parts))
        self.assertEqual("".join(parts), report)


    def test_manual_ai_date_reader_does_not_write(self):
        before = {
            name: sheet.get_all_values()
            for name, sheet in self.book.tabs.items()
        }
        data = m.read_ai_report_inputs(self.book, date(2026, 10, 20))
        self.assertIsNotNone(data)
        report, record, morning, games = data
        self.assertEqual(record["date"], date(2026, 10, 20))
        self.assertIn("🏔️ Everest", report)
        self.assertIn("🔎 Значимые изменения", report)
        self.assertNotIn("исторические данные не подключены", report)
        self.assertIsNone(m.read_ai_report_inputs(self.book, date(2026, 10, 18)))
        self.assertEqual(before, {
            name: sheet.get_all_values()
            for name, sheet in self.book.tabs.items()
        })


class OpenAIHypothesisTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.target = {
            "date": date(2026, 10, 20),
            "fields": {
                "1. Во сколько лег спать?": "22:00",
                "2. Во сколько окончательно проснулся?": "08:00",
                "6. Через 30–60 минут": "заметная сонливость",
                "7. Что сейчас": "признаки болезни",
                "8. Когда читаешь": "часто",
                "9. Простые утренние дела": "заметно тяжелее обычного",
                "Сегодня": "игра",
                "Город": "Владивосток",
                "Имя": "Алексей Жуков",
                "Свободный комментарий": "личный медицинский комментарий",
            },
        }
        self.base_report = (
            "📊 Everest | Алексей Жуков | 20.10.2026\n\n"
            "ФАКТ\nУтреннее состояние.\n\n"
            "НАБЛЮДЕНИЕ\n3 дня: данных мало.\n\n"
            "ГИПОТЕЗА\nОбщий шаблон.\n\n"
            "ПРЕДЛАГАЕМОЕ РЕШЕНИЕ\nОбщий шаблон действия."
        )

    def _context(self):
        return ai.build_context(
            self.target, [self.target], [],
            m.get, m.sleep_window, m.game_hours,
            m.automation, m.concrete_lapse, m.quality_drop,
        )

    def test_anonymized_context_contains_no_identifiers_or_raw_answers(self):
        data = json.dumps(self._context(), ensure_ascii=False)
        self.assertNotIn("Алексей", data)
        self.assertNotIn("Жуков", data)
        self.assertNotIn("Владивосток", data)
        self.assertNotIn("медицинский комментарий", data)
        self.assertNotIn("2026-10-20", data)
        self.assertEqual(len(self._context()["timeline"]), 1)
        self.assertEqual(self._context()["timeline"][0]["morning"]["sleep_window_hours_not_measured_sleep"], 10)

    def test_invalid_or_incomplete_openai_response_falls_back(self):
        self.assertIsNone(ai.parse_response({"status": "incomplete", "output": []}))
        self.assertIsNone(ai.parse_response({"status": "completed", "output": [
            {"content": [{"type": "output_text", "text": '{"hypotheses": []}'}]}
        ]}))
        self.assertEqual(ai.render_analysis(self.base_report, None), self.base_report)

    def test_valid_individual_hypotheses_replace_templates_preserve_facts(self):
        obj = {
            "hypotheses": [{
                "version": "Возможно, сложные решения требуют больше усилий.",
                "evidence": "Отмечены затруднения внимания и простых решений.",
                "alternative": "Состояние может быть связано с недомоганием.",
                "check_next": "Сравнить с ближайшим утром после отдыха.",
            }],
            "proposed_action": "Согласовать с куратором осторожный план.",
            "limitations": "Наблюдений всего один день.",
        }
        api_body = {"status": "completed", "output": [
            {"content": [{"type": "output_text", "text": json.dumps(obj)}]}
        ]}
        parsed = ai.parse_response(api_body)
        enriched = ai.render_analysis(self.base_report, parsed)
        self.assertIn("ФАКТ\nУтреннее состояние", enriched)
        self.assertIn("НАБЛЮДЕНИЕ\n3 дня: данных мало", enriched)
        self.assertIn("ГИПОТЕЗА (OpenAI API", enriched)
        self.assertIn("Возможно, сложные решения", enriched)
        self.assertIn("Альтернатива:", enriched)
        self.assertIn("Проверить:", enriched)
        self.assertIn("Решение о фактической нагрузке остаётся за куратором", enriched)
        self.assertNotIn("ГИПОТЕЗА\nОбщий шаблон.", enriched)

    async def test_no_key_no_external_call(self):
        with patch.dict(os.environ, {
            "ZHUKOV_MONITOR_OPENAI_ENABLED": "true",
            "OPENAI_API_KEY": "",
        }):
            with patch("zhukov_ai.aiohttp.ClientSession") as session:
                result = await ai.enrich_report(
                    self.base_report, self.target, [self.target], [],
                    m.get, m.sleep_window, m.game_hours, m.automation,
                    m.concrete_lapse, m.quality_drop,
                )
            session.assert_not_called()
        self.assertIn("🏔️ Everest", result)
        self.assertIn("AI-анализ недоступен", result)
        self.assertNotIn("ФАКТ\n", result)

    def test_telegram_html_bolds_cost_only_and_escapes_report_content(self):
        plain = (
            "План: A < B & C\n"
            "ПРЕДЛАГАЕМОЕ РЕШЕНИЕ\nОставить план.\n\n"
            "💳 OpenAI API: ≈ $0.000690 за этот анализ "
            "(по токенам; фактическое списание может отличаться)."
        )
        rendered = m.telegram_report_html(plain)
        self.assertIn("A &lt; B &amp; C", rendered)
        self.assertIn("💳 <b>OpenAI API</b>: ≈ <b>$0.000690</b>", rendered)
        self.assertIn("ПРЕДЛАГАЕМОЕ РЕШЕНИЕ", rendered)
        self.assertNotIn("<b>ПРЕДЛАГАЕМОЕ РЕШЕНИЕ</b>", rendered)
        no_usage = m.telegram_report_html(
            "💳 OpenAI API: стоимость этого запроса не удалось рассчитать"
        )
        self.assertIn("💳 <b>OpenAI API</b>: стоимость", no_usage)
        self.assertNotIn("<b>$", no_usage)
        safety = m.telegram_report_html(
            "<b>Непроверенный HTML</b>\n"
            "💳 OpenAI API: ≈ $0.000690"
        )
        self.assertIn("&lt;b&gt;Непроверенный HTML&lt;/b&gt;", safety)

    def test_openai_cost_uses_actual_input_output_and_cached_tokens(self):
        response = {
            "model": "gpt-4.1-mini-2025-04-14",
            "usage": {
                "input_tokens": 1000,
                "input_tokens_details": {"cached_tokens": 100},
                "output_tokens": 200,
                "total_tokens": 1200,
            },
        }
        self.assertEqual(
            format(ai.calculate_response_cost_usd(response), ".6f"), "0.000690"
        )
        self.assertIn(
            "💳 OpenAI API: ≈ $0.000690",
            ai.response_cost_footer(response)
        )
        no_cache = {
            "model": "gpt-4.1-mini",
            "usage": {"input_tokens": 1500, "output_tokens": 300},
        }
        self.assertEqual(
            format(ai.calculate_response_cost_usd(no_cache), ".6f"), "0.001080"
        )
        gpt_five = {
            "model": "gpt-5-mini",
            "usage": {"input_tokens": 1000, "output_tokens": 200},
        }
        self.assertEqual(
            format(ai.calculate_response_cost_usd(gpt_five), ".6f"), "0.000650"
        )

    def test_api_cost_is_not_invented_without_reliable_usage_or_rate(self):
        valid = {"input_tokens": 1000, "output_tokens": 200}
        unknown = {"model": "unknown-model-2035", "usage": valid}
        self.assertIsNone(ai.calculate_response_cost_usd(unknown))
        self.assertIn("стоимость неизвестна", ai.response_cost_footer(unknown))
        self.assertIsNone(
            ai.calculate_response_cost_usd({"model": "gpt-4.1-mini"})
        )
        self.assertIsNone(ai.calculate_response_cost_usd({
            "model": "gpt-4.1-mini",
            "usage": {"input_tokens": 1000, "output_tokens": 200,
                      "input_tokens_details": {"cached_tokens": 1001}},
        }))
        self.assertIsNone(ai.calculate_response_cost_usd({
            "model": "gpt-4.1-mini", "usage": valid,
            "service_tier": "priority",
        }))
        self.assertIsNone(ai.calculate_response_cost_usd({
            "model": "gpt-4.1-mini",
            "usage": {"input_tokens": True, "output_tokens": 10},
        }))

    async def test_api_payload_uses_structured_output_and_store_false(self):
        reply = {
            "status": "completed",
            "model": "gpt-4.1-mini-2025-04-14",
            "usage": {
                "input_tokens": 1000,
                "input_tokens_details": {"cached_tokens": 100},
                "output_tokens": 200,
            },
            "output": [{"content": [{"type": "output_text", "text": json.dumps({
                "observation": "Резкого ухудшения по доступным данным нет.",
                "hypothesis": "",
                "curator_action": "Сохранить план."
            })}]}],
        }
        class FakeResponse:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): return False
            def raise_for_status(self): pass
            async def json(self): return reply
        class FakeSession:
            def __init__(self): self.payload = None
            async def __aenter__(self): return self
            async def __aexit__(self, *args): return False
            def post(self, url, **kwargs):
                self.payload = kwargs["json"]
                self.url = url
                return FakeResponse()
        fake = FakeSession()
        with patch.dict(os.environ, {
            "ZHUKOV_MONITOR_OPENAI_ENABLED": "true",
            "OPENAI_API_KEY": "TEST_FAKE_KEY_NOT_REAL",
        }):
            with patch("zhukov_ai.aiohttp.ClientSession", return_value=fake):
                enriched = await ai.enrich_report(
                    self.base_report, self.target, [self.target], [],
                    m.get, m.sleep_window, m.game_hours,
                    m.automation, m.concrete_lapse, m.quality_drop,
                )
        self.assertIn("Проверяемой закономерности пока не выявлено", enriched)
        self.assertTrue(enriched.endswith("💳 OpenAI API: ≈ $0.000690"))
        self.assertGreater(enriched.index("💳 OpenAI API"), enriched.index("🎯 Рекомендация куратору"))
        self.assertLess(len(enriched), 900)
        self.assertNotIn("за этот анализ", enriched)
        self.assertNotIn("ФАКТ\n", enriched)
        self.assertEqual(fake.url, "https://api.openai.com/v1/responses")
        self.assertIs(fake.payload["store"], False)
        self.assertEqual(fake.payload["text"]["format"]["type"], "json_schema")
        self.assertNotIn("Жуков", fake.payload["input"])
        self.assertNotIn("Владивосток", fake.payload["input"])

    async def test_manual_ai_refuses_disabled_or_missing_configuration(self):
        args = (self.base_report, self.target, [self.target], [],
                m.get, m.sleep_window, m.game_hours,
                m.automation, m.concrete_lapse, m.quality_drop)
        with patch.dict(os.environ, {
            "ZHUKOV_MONITOR_OPENAI_ENABLED": "false",
            "OPENAI_API_KEY": "test-placeholder",
        }):
            with self.assertRaisesRegex(ai.AIAnalysisError, "AI выключен"):
                await ai.enrich_report(*args, strict=True)
            fallback = await ai.enrich_report(*args)
            self.assertIn("🏔️ Everest", fallback)
            self.assertIn("AI-анализ недоступен", fallback)
        with patch.dict(os.environ, {
            "ZHUKOV_MONITOR_OPENAI_ENABLED": "true",
            "OPENAI_API_KEY": "",
        }):
            with self.assertRaisesRegex(ai.AIAnalysisError, "OPENAI_API_KEY"):
                await ai.enrich_report(*args, strict=True)

    async def test_manual_ai_rejects_incomplete_api_response(self):
        class Response:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): return False
            def raise_for_status(self): pass
            async def json(self): return {"status": "incomplete", "output": []}
        class Session:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): return False
            def post(self, *args, **kwargs): return Response()
        args = (self.base_report, self.target, [self.target], [],
                m.get, m.sleep_window, m.game_hours,
                m.automation, m.concrete_lapse, m.quality_drop)
        with patch.dict(os.environ, {
            "ZHUKOV_MONITOR_OPENAI_ENABLED": "true",
            "OPENAI_API_KEY": "test-placeholder",
        }):
            with patch("zhukov_ai.aiohttp.ClientSession", return_value=Session()):
                with self.assertRaisesRegex(ai.AIAnalysisError, "не вернул полный"):
                    await ai.enrich_report(*args, strict=True)
                fallback = await ai.enrich_report(*args)
                self.assertIn("🏔️ Everest", fallback)
                self.assertIn("AI-анализ недоступен", fallback)

    async def test_auto_report_formats_card_cost_as_html(self):
        env = patch.dict(os.environ, {
            "TELEGRAM_USER_ID": "900",
            "ZHUKOV_CURATOR_CHAT_ID": "900",
            "ZHUKOV_TELEGRAM_USER_ID": "901",
            "ZHUKOV_SHEET_TIMEZONE": "Europe/Moscow",
        })
        with env:
            ws = Sheet("журнал", [m.HEADERS])
            bot = Bot()
            await m._send_once(bot, ws, set(), (
                "zhukov:analysis:2026-10-20", "ANALYSIS",
                date(2026, 10, 20),
                datetime(2026, 10, 20, 8, tzinfo=TZ),
                "Предложение куратору.\n\n"
                "💳 OpenAI API: ≈ $0.000690 за этот анализ.",
            ))
            self.assertEqual(bot.parse_modes, ["HTML"])
            self.assertIn(
                "💳 <b>OpenAI API</b>: ≈ <b>$0.000690</b>",
                bot.messages[0][1],
            )
            self.assertIn(
                "💳 OpenAI API: ≈ $0.000690",
                ws.rows[1][4],
            )
            self.assertNotIn("<b>", ws.rows[1][4])
            self.assertEqual(ws.rows[1][5], "SENT")

    async def test_long_ai_report_delivered_in_multiple_messages(self):
        env = patch.dict(os.environ, {
            "TELEGRAM_USER_ID": "900",
            "ZHUKOV_CURATOR_CHAT_ID": "900",
            "ZHUKOV_TELEGRAM_USER_ID": "901",
            "ZHUKOV_SHEET_TIMEZONE": "Europe/Moscow",
        })
        with env:
            longtext = "ГИПОТЕЗА проверяемая\n" * 420
            ws = Sheet("журнал", [m.HEADERS])
            bot = Bot()
            await m._send_once(bot, ws, set(), (
                "zhukov:analysis:2026-10-20", "ANALYSIS",
                date(2026, 10, 20),
                datetime(2026, 10, 20, 8, tzinfo=TZ),
                longtext,
            ))
            self.assertGreater(len(bot.messages), 1)
            self.assertTrue(all(chat == 900 for chat, _ in bot.messages))
            self.assertEqual("\n".join(text for _, text in bot.messages), longtext)
            self.assertEqual(ws.rows[1][5], "SENT")


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
        self.assertEqual(sum("🔎 Значимые изменения" in x for x in messages), 1)
        await m.tick(self.bot, self.book, datetime(2026, 10, 2, 17, tzinfo=TZ))
        self.assertEqual(len(self.bot.messages), 2)

    async def test_timely_report_does_not_trigger_missed_alert(self):
        self.book.worksheet("Утро — ответы").append_row(
            morning("02.10.2026 13:59:00", "02.10.2026")
        )
        await m.tick(self.bot, self.book, datetime(2026, 10, 2, 15, tzinfo=TZ))
        self.assertEqual(len(self.bot.messages), 1)
        self.assertIn("🎯 Рекомендация куратору", self.bot.messages[0][1])

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
        self.assertIn("🔎 Значимые изменения", self.bot.messages[0][1])


if __name__ == "__main__":
    unittest.main()
