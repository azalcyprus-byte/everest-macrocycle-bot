import unittest
from datetime import date
from decimal import Decimal

import zhukov_morning as morning
import zhukov_monitor as monitor
from zhukov_plan import PlannedLoadCode


def rec(day, bed="00:00", wake="08:30", awakenings="не просыпался",
        onset="До 15 минут", rise="обычно", sleepy="нет",
        body="ничего необычного", attention="нет", decisions="как обычно"):
    return {"date": date.fromisoformat(day), "fields": {
        "1. Во сколько лег спать?": bed,
        "2. Во сколько окончательно проснулся?": wake,
        "3. Сколько раз просыпался за ночь?": awakenings,
        "4. Как быстро уснул?": onset,
        "5. Как встал?": rise,
        "6. Через 30–60 минут": sleepy,
        "7. Что сейчас": body,
        "8. Когда читаешь": attention,
        "9. Простые решения": decisions,
    }}


class MorningReportTests(unittest.TestCase):
    def test_condition_index_uses_only_morning_answers(self):
        self.assertEqual(morning.condition_index(rec("2026-10-26"), monitor.get), 97)
        weak = rec("2026-10-26", rise="тяжело", sleepy="заметная, могу снова уснуть",
                   attention="часто", decisions="заметно тяжелее обычного")
        self.assertEqual(morning.condition_index(weak, monitor.get), 51)

    def test_bedtime_average_crosses_midnight_correctly(self):
        history = [rec("2026-10-24", bed="23:30"), rec("2026-10-25", bed="00:30")]
        current = rec("2026-10-26", bed="01:00")
        _, average, delta, count = morning.recent_bedtime(current, history + [current], monitor.get)
        self.assertEqual(count, 2)
        self.assertLess(abs(average), 1)
        self.assertAlmostEqual(delta, 60, delta=1)

    def test_chart_never_treats_morning_game_choice_as_fact(self):
        target = rec("2026-10-21")
        target["fields"]["Сегодня"] = "игра"
        plans = {
            date(2026, 10, 20): PlannedLoadCode("L", Decimal("3.5")),
            date(2026, 10, 21): PlannedLoadCode("M", Decimal("5.5")),
        }
        series = morning.chart_series(target, [target], [], monitor.get, plans)
        self.assertEqual(series[-1]["plan"], 9.0)
        self.assertEqual(series[-1]["fact"], 0.0)
        png = morning.render_chart_png(series).read()
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_compact_report_contains_required_fields(self):
        prior = rec("2026-10-25", bed="00:00")
        current = rec("2026-10-26", bed="01:30", wake="10:00")
        text = morning.render_report(
            current, [prior, current], monitor.get,
            PlannedLoadCode("L+", Decimal("4.5")), "сохранить план.",
        )
        self.assertIn("Индекс утра:", text)
        self.assertIn("S 25 + Z 10 + V 32 + C 30", text)
        self.assertIn("к вчера", text)
        self.assertIn("План: L+ · 4,5 ч", text)
        self.assertIn("Ключевой риск по утренней анкете:", text)
        self.assertIn("отклонение +1ч30м", text)
        self.assertIn("окно сна 8ч30м", text)
        self.assertIn("среднее предыдущих 1 анкет 8ч30м", text)
        self.assertLess(len(text), 1024)


if __name__ == "__main__":
    unittest.main()
