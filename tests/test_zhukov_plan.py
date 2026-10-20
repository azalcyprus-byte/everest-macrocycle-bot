"""Date-scoped macrocycle plan and game-declaration regression tests."""
import os
import unittest
from datetime import date
from unittest.mock import patch

import zhukov_brief as brief
import zhukov_plan as plan


def record(day, activity):
    return {
        "date": date.fromisoformat(day),
        "fields": {
            "Сегодня": activity,
            "6. Через 30–60 минут": "нет",
            "8. Когда читаешь": "нет",
            "9. Простые решения": "как обычно",
            "7. Что сейчас": "ничего необычного",
        },
    }


def get_value(rec, prefix, default="нет данных"):
    for key, value in rec["fields"].items():
        if key.startswith(prefix):
            return value
    return default


class PlannedLoadTests(unittest.TestCase):
    def setUp(self):
        self.rows = [
            ["Дата", "Фаза", "Day Type", "Game MIN (ч)"],
            ["20.10", "Стабилизация", "O", "0.0"],
            ["21.10", "Стабилизация", "L", "3.0"],
            ["22.10", "Интенсив", "M", "3.0"],
            ["26.10", "Интенсив", "L+", "3.0"],
            ["03.11", "Peak", "T", "3.0"],
        ]
        self.types = plan.parse_plan_rows(self.rows, year=2026)

    def test_uses_exact_official_planned_codes_by_date(self):
        expected = {
            "2026-10-20": "O",
            "2026-10-21": "L",
            "2026-10-22": "M",
            "2026-10-26": "L+",
            "2026-11-03": "T",
        }
        for day, code in expected.items():
            with self.subTest(day=day):
                self.assertEqual(
                    plan.planned_type_for(self.types, date.fromisoformat(day)),
                    code,
                )

    def test_never_reuses_a_prior_year_or_guesses_unlisted_days(self):
        self.assertIsNone(plan.planned_type_for(self.types, date(2027, 10, 20)))
        self.assertIsNone(plan.planned_type_for(self.types, date(2026, 11, 11)))
        self.assertIsNone(plan.planned_type_for({}, date(2026, 10, 20)))
        self.assertEqual(
            brief.render(
                record("2026-11-11", "игра"),
                [record("2026-11-11", "игра")],
                [], get_value, plan_type=None,
            ).splitlines()[1],
            "📅 План нагрузки: не указан",
        )

    def test_conflicting_rows_do_not_create_fictional_plan(self):
        rows = self.rows + [["20.10", "Стабилизация", "T", "10.0"]]
        result = plan.parse_plan_rows(rows, year=2026)
        self.assertNotIn(date(2026, 10, 20), result)
        self.assertEqual(result[date(2026, 10, 26)], "L+")
        with self.assertRaises(ValueError):
            plan.parse_plan_rows([["Дата", "Фаза"], ["20.10", "X"]], year=2026)

    def test_game_declaration_is_independent_of_load_code(self):
        samples = [
            ("2026-10-20", "перелёт", "O", "Нет (перелёт)"),
            ("2026-10-21", "игра", "L", "Да (заявлено утром)"),
            ("2026-10-22", "отдых", "M", "Нет (отдых)"),
            ("2026-10-26", "игра", "L+", "Да (заявлено утром)"),
            ("2026-11-03", "игра", "T", "Да (заявлено утром)"),
        ]
        for day, activity, code, status in samples:
            with self.subTest(day=day):
                current = record(day, activity)
                with patch.dict(os.environ, {"GOOGLE_SERVICE_ACCOUNT_JSON": ""}):
                    txt = brief.render(
                        current, [current], [], get_value, plan_type=code,
                    )
                self.assertIn("📅 План нагрузки: " + code, txt)
                self.assertIn("🎮 Игровой день: " + status, txt)
                self.assertIn("🎯 Рекомендация куратору", txt)
                self.assertLess(len(txt), 950)

    def test_missing_activity_is_not_mislabeled_as_no_game(self):
        current = record("2026-10-21", "")
        with patch.dict(os.environ, {"GOOGLE_SERVICE_ACCOUNT_JSON": ""}):
            txt = brief.render(
                current, [current], [], get_value, plan_type="L",
            )
        self.assertIn("📅 План нагрузки: L", txt)
        self.assertIn("🎮 Игровой день: не указан", txt)

    def test_html_bolds_two_new_labels_safely(self):
        from zhukov_monitor import telegram_report_html
        current = record("2026-10-20", "перелёт")
        txt = brief.render(
            current, [current], [], get_value, plan_type="O",
        )
        rich = telegram_report_html(txt)
        self.assertIn("<b>📅 План нагрузки:</b> O", rich)
        self.assertIn("<b>🎮 Игровой день:</b> Нет (перелёт)", rich)
        self.assertNotIn("<script>", rich)


if __name__ == "__main__":
    unittest.main()
