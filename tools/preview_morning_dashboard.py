"""Generate visibly watermarked fixtures locally; never sends to Telegram."""
from datetime import timedelta
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.test_zhukov_dashboard import DAY, morning, evening, bed
from zhukov_dashboard import build_report, render_png


destination = Path(sys.argv[1])
destination.mkdir(parents=True, exist_ok=True)
for scenario, game, hours in (("training", "Нет", ""), ("game", "Да", "8.5"), ("rest", "Нет", "0")):
    mornings = [morning(DAY - timedelta(days=i)) for i in (0, 1, 2, 4, 6)]
    mornings[1]["fields"]["6. Через 30–60 минут"] = "немного"
    history = [evening(DAY - timedelta(days=i), game, hours) for i in (1, 2, 3)]
    plans = {
        DAY: {"type": "M", "heavy": 90, "light": 30, "game": None, "physical": None},
        DAY - timedelta(days=1): {"type": "L", "heavy": 60, "light": 45, "game": 8.0 if game == "Да" else 0.0},
    }
    if scenario == "rest":
        plans = {}
        history = [evening(heavy="", light="", physical="")]
    report = build_report(mornings[0], mornings, history,
                          [bed(item["date"]) for item in mornings] if scenario != "rest" else [], plans)
    image = render_png(report, demo=True)
    path = destination / f"everest_morning_{scenario}.png"
    path.write_bytes(image.getvalue())
    print(path)
