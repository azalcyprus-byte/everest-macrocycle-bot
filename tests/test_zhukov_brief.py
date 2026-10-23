"""Regression coverage for short, evidence-oriented Everest curator briefs."""
import json
import unittest
from datetime import date

import zhukov_brief as brief
import zhukov_monitor as monitor


def make_morning(day, sleepiness="нет", attention="нет", decisions="как обычно",
                 body="ничего необычного"):
    return {
        "date": date.fromisoformat(day),
        "fields": {
            "1. Во сколько лег спать?": "23:15",
            "2. Во сколько окончательно проснулся?": "07:45",
            "3. Сколько раз просыпался за ночь?": "2",
            "4. Как быстро уснул?": "долго, около 40 минут",
            "5. Как встал?": "тяжело",
            "6. Через 30–60 минут": sleepiness,
            "8. Когда читаешь": attention,
            "9. Простые решения": decisions,
            "7. Что сейчас": body,
        },
    }


class BriefTests(unittest.TestCase):
    def test_sparse_day_is_one_compact_report_without_raw_checklist(self):
        record = make_morning("2026-10-20", attention="иногда")
        text = brief.render(record, [record], [], monitor.get)
        self.assertLess(len(text), 1400)
        self.assertIn("🏔️ Everest | Алексей Жуков | 20.10.2026", text)
        self.assertIn("✅ Нет подтверждённых оснований менять план", text)
        self.assertIn("🔎 Значимые изменения", text)
        self.assertIn("💡 Гипотеза", text)
        self.assertIn("🎯 Рекомендация куратору", text)
        self.assertIn("ℹ️ Надёжность: за 7 дней 1/7 анкет", text)
        self.assertIn("Проверяемой закономерности пока не выявлено.", text)
        self.assertNotIn("Весь наблюдаемый период", text)
        self.assertNotIn("Источник:", text)
        self.assertIn("😴 Сон:", text)
        self.assertIn("🧠 После пробуждения:", text)
        self.assertIn("отход ко сну: 23:15", text)
        self.assertIn("засыпание: долго, около 40 минут", text)
        self.assertIn("окно между указанными временами: 8.5 ч (не фактический сон)", text)
        self.assertIn("сонливость через 30–60 минут: нет", text)
        self.assertNotIn("ФАКТ\n", text)
        self.assertNotIn("⚠️", text)

    def test_ai_cannot_invent_flight_hypothesis_on_isolated_normal_day(self):
        record = make_morning("2026-10-20", attention="иногда")
        insight = {
            "observation": "Перелёт точно привёл к накоплению усталости.",
            "hypothesis": "Стресс перелёта снижает работоспособность.",
            "curator_action": "Снизить игровую нагрузку на 50%.",
        }
        text = brief.render(record, [record], [], monitor.get, insight)
        self.assertNotIn("Перелёт точно", text)
        self.assertNotIn("Стресс перелёта", text)
        self.assertNotIn("Снизить", text)
        self.assertIn("Проверяемой закономерности пока не выявлено", text)
        self.assertIn("Сохранить согласованный план", text)

    def test_alert_on_two_independent_signals_does_not_auto_reduce_load(self):
        old = make_morning("2026-10-19")
        record = make_morning(
            "2026-10-20", attention="часто",
            decisions="заметно тяжелее обычного",
        )
        text = brief.render(
            record, [old, record], [], monitor.get,
            {
                "observation": "Есть одновременное ухудшение нескольких индикаторов.",
                "hypothesis": "Возможно, нарушения концентрации связаны с нагрузкой.",
                "curator_action": "Снизить нагрузку автоматически.",
            }
        )
        self.assertIn("🟠 Нужна проверка состояния", text)
        self.assertIn("внимание, темп решений", text)
        self.assertNotIn("Снизить нагрузку автоматически", text)
        self.assertIn("Проверить состояние", text)

    def test_corroborated_data_allows_one_short_specific_hypothesis(self):
        days = [
            make_morning(f"2026-10-{i:02d}") for i in (17, 18, 19)
        ]
        record = make_morning("2026-10-20", sleepiness="заметная")
        insight = {
            "observation": "В двух последних наблюдениях внимание хуже на длительных сессиях.",
            "hypothesis": "Возможно, длительные игровые сессии связаны с ухудшением внимания.",
            "curator_action": "Сравнить внимание после обычной и длинной игровой сессии.",
        }
        text = brief.render(record, days + [record], [], monitor.get, insight)
        self.assertIn("🟡 Есть сигнал для контроля", text)
        self.assertIn(insight["observation"], text)
        self.assertIn(insight["hypothesis"], text)
        self.assertIn(insight["curator_action"], text)
        self.assertIn("4/7 анкет", text)
        self.assertLess(len(text), 1400)

    def test_illness_is_distinguished_from_game_fatigue(self):
        record = make_morning("2026-10-20", body="признаки болезни")
        text = brief.render(record, [record], [], monitor.get)
        self.assertIn("🟠 Нужна проверка состояния", text)
        self.assertIn("самочувствие отдельно от игровой усталости", text)
        self.assertNotIn("Снизить", text)

    def test_structured_schema_and_text_limits(self):
        self.assertEqual(set(brief.SCHEMA["required"]),
                         {"observation", "hypothesis", "curator_action"})
        malformed = {
            "status": "completed",
            "output": [{"content": [{"type": "output_text", "text": '{"foo":"bar"}'}]}],
        }
        self.assertIsNone(brief.parse_response(malformed))
        sample = {
            "observation": "1. Наблюдение " * 45,
            "hypothesis": "",
            "curator_action": "Обсудить план.",
        }
        response = {
            "status": "completed",
            "output": [{"content": [{"type": "output_text", "text": json.dumps(sample)}]}],
        }
        parsed = brief.parse_response(response)
        self.assertIsNotNone(parsed)
        self.assertLessEqual(len(parsed["observation"]), 190)
        self.assertNotIn("\n", parsed["observation"])
        self.assertFalse(parsed["observation"].startswith("1."))

    def test_html_bolds_headers_and_escapes_untrusted_fragments(self):
        record = make_morning("2026-10-20")
        text = brief.render(record, [record], [], monitor.get)
        html_text = monitor.telegram_report_html(text)
        self.assertIn("<b>🏔️ Everest | Алексей Жуков | 20.10.2026</b>", html_text)
        self.assertIn("<b>✅ Нет подтверждённых оснований менять план</b>", html_text)
        self.assertIn("<b>🔎 Значимые изменения</b>", html_text)
        self.assertIn("<b>🎯 Рекомендация куратору</b>", html_text)
        self.assertIn("<b>ℹ️ Надёжность:</b>", html_text)
        escaped = monitor.telegram_report_html(
            "🔎 Значимые изменения\nТест <bad> & неизвестно\n"
            "💳 OpenAI API: ≈ $0.000690"
        )
        self.assertIn("&lt;bad&gt; &amp; неизвестно", escaped)
        self.assertIn("<b>OpenAI API</b>", escaped)
        self.assertIn("<b>$0.000690</b>", escaped)


if __name__ == "__main__":
    unittest.main()
