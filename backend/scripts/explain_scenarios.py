"""Print the routing, tasks and vendor message for each fixture scenario (gold readings, no database)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

from test_scenarios_logic import SCEN, evaluate  # noqa: E402

from app.pipeline.decide import compose_vendor_message  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")  # titles and messages use arrows and dashes
only = sys.argv[1:] or list(SCEN)
for key in only:
    s = SCEN[key]
    inp, cmp, checks, r = evaluate(key)
    print(f"\n=== {key}: {s['title']}\n    expected: {s['expect']}\n    routed:   {r.outcome} — {r.headline}")
    for c in checks:
        if c.status not in ("satisfied", "not_applicable"):
            print(f"    [{c.status:13s}] {c.key:40s} owner={c.owner or '-':8s} {c.reason[:150]}")
    m = compose_vendor_message(r, s["form"]["business"]["legal_name"], 1, [])
    print(f"    MESSAGE: {m.subject}")
    for it in m.items:
        print(f"      - ({it['type']}) {it['text'][:220]}")
