"""Measure live extraction against independently authored gold labels.

Usage: python scripts/extraction_spike.py [--parallel N] [file names...]   (default: a representative set)
Results are merged into fixtures/out/spike_report.json (latest run per file wins; totals cover every
file in the report). Never prints an API key.
"""
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.extraction.service import FIXTURE_DIR, LiveReader  # noqa: E402
from app.india.validators import clean_identifier  # noqa: E402
from app.pdf.inspect import inspect_pdf  # noqa: E402

DEFAULT = ["nimbus_bank_letter.pdf", "nimbus_gst.pdf", "tejas_cheque.pdf", "saffron_director_pan.pdf",
           "kaveri_pan_scanned.pdf", "kaveri_bank_mixed.pdf", "kaveri_cheque_unreadable_scan.pdf", "indigo_incorporation.pdf"]
VERDICTS = ("correct", "abstained_ok", "fabricated", "missed", "wrong")
REPORT = FIXTURE_DIR / "spike_report.json"


def norm(key, v):
    if v is None:
        return None
    if key in {"pan", "gstin", "registration_number", "account_number", "ifsc", "udyam_number"}:
        return clean_identifier(v)
    words = str(v).upper().replace(",", " ").replace(".", " ").split()
    if key == "branch" and words and words[-1] == "BRANCH":  # cheques print "Baner, Pune Branch"; gold says "Baner, Pune"
        words = words[:-1]
    return " ".join(words)


def measure(reader, gold, name):
    g = gold[name]
    data = (FIXTURE_DIR / "documents" / name).read_bytes()
    info = inspect_pdf(data, 10 * 2**20, 10)
    t = time.time()
    out = reader.read(data, [p.kind for p in info.pages])
    secs = time.time() - t
    row = {"file": name, "measured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "layout": info.layout,
           "status": out.status, "seconds": round(secs, 1), "attempts": [a.__dict__ for a in out.attempts],
           "error": out.error, "fields": {}}
    if out.reading:
        row["detected_type"] = out.reading.detected_type
        row["type_ok"] = out.reading.detected_type == g["type"]
        for key, exp in g["fields"].items():
            f = out.reading.fields.get(key)
            got = f.normalized if f and f.status == "found" else None
            if key in g.get("unreadable", []) or exp is None:
                verdict = "abstained_ok" if got is None else "fabricated"
            elif got is None:
                verdict = "missed"
            else:
                verdict = "correct" if norm(key, got) == norm(key, exp) else "wrong"
            row["fields"][key] = {"expected": exp, "got": got, "status": f.status if f else None,
                                  "provenance": f.provenance if f else None, "verdict": verdict}
        row["quality_issues"] = out.reading.quality_issues
    bad = {k: v for k, v in row["fields"].items() if v["verdict"] not in ("correct", "abstained_ok")}
    keys_used = sorted({a.target for a in out.attempts})
    print(f"{name:38s} {info.layout:8s} {out.status:8s} {secs:6.1f}s type_ok={row.get('type_ok')} issues={len(bad)} "
          f"via {', '.join(keys_used)} {('ERROR ' + str(out.error)) if out.error else ''}", flush=True)
    for k, v in bad.items():
        print(f"      {k}: expected {v['expected']!r} got {v['got']!r} ({v['status']}, {v['verdict']})", flush=True)
    return row


def main(argv):
    parallel = 1
    if "--parallel" in argv:
        i = argv.index("--parallel")
        parallel = int(argv[i + 1])
        argv = argv[:i] + argv[i + 2:]
    files = argv or DEFAULT
    gold = json.loads((FIXTURE_DIR / "gold_labels.json").read_text(encoding="utf-8"))
    reader = LiveReader()
    print("model chain:", reader.describe(), f"| parallel {parallel}", flush=True)
    with ThreadPoolExecutor(max_workers=parallel) as pool:
        rows = list(pool.map(lambda n: measure(reader, gold, n), files))

    old = json.loads(REPORT.read_text(encoding="utf-8")) if REPORT.exists() else {"rows": []}
    merged = {r["file"]: r for r in old.get("rows", [])}
    for r in rows:
        if r["status"] != "failed" or r["file"] not in merged:  # a provider failure doesn't erase an earlier measurement
            merged[r["file"]] = r
    totals = {"fields": 0, **{v: 0 for v in VERDICTS}}
    for r in merged.values():
        for f in r["fields"].values():
            totals["fields"] += 1
            totals[f["verdict"]] += 1
    run_totals = {"fields": 0, **{v: 0 for v in VERDICTS}}
    for r in rows:
        for f in r["fields"].values():
            run_totals["fields"] += 1
            run_totals[f["verdict"]] += 1
    print("this run:", run_totals)
    print(f"report ({len(merged)} files):", totals)
    REPORT.write_text(json.dumps({"totals": totals, "rows": list(merged.values())}, indent=2, default=str), encoding="utf-8")


if __name__ == "__main__":
    main(sys.argv[1:])
