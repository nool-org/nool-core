"""Measured AI cost per feature and per student, from the services' own cost
log lines - see scripts/cost_report.md.

    python scripts/cost_report.py [LOG_FILE] [--since YYYY-MM-DD] [--until YYYY-MM-DD] [--csv]

Without LOG_FILE it reads `docker compose logs --no-color generation colearner`.
Lines used: generation_call_cost and grade_answer_cost / regrade_cost (a JSON
"cost" object), realtime_call_cost (key=value message, one per live voice
call) joined by call_id to realtime_call_started (which names the student).
Days are IST calendar days. Stdlib only, read-only.
"""

import argparse
import csv
import json
import math
import re
import subprocess
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

USD_TO_INR_RATE = 86.5  # same indicative rate as colearner/generation
IST = timezone(timedelta(hours=5, minutes=30))
_KV = re.compile(r"(\w+)=(\S*)")


def _events(lines):
    """(day, feature, student, usd) per AI call, realtime calls joined to their start line."""
    started, calls = {}, []
    for line in lines:
        brace = line.find("{")
        if brace < 0:
            continue
        try:
            rec = json.loads(line[brace:])
        except ValueError:
            continue
        msg, ts = str(rec.get("message", "")), rec.get("timestamp")
        if not ts:
            continue
        day = datetime.fromisoformat(ts).astimezone(IST).date()
        kind = msg.split(" ", 1)[0]
        cost = rec.get("cost") or {}
        if kind == "generation_call_cost":
            calls.append((day, cost.get("feature") or cost.get("endpoint") or "unknown", cost.get("student"),
                          cost.get("usd") or 0.0))
        elif kind == "grade_answer_cost":
            calls.append((day, cost.get("feature") or "practice_grade", cost.get("student"),
                          cost.get("turn_cost_usd") or 0.0))
        elif kind == "regrade_cost":
            calls.append((day, cost.get("feature") or "voice_regrade", cost.get("student"), cost.get("usd") or 0.0))
        elif kind == "realtime_call_started":
            started[dict(_KV.findall(msg)).get("call_id")] = dict(_KV.findall(msg))
        elif kind == "realtime_call_cost":
            kv = dict(_KV.findall(msg))
            start = started.get(kv.get("call_id"), {})
            calls.append((day, start.get("feature") or "voice_test", start.get("student") or None,
                          float(kv.get("total_usd") or 0)))
    return calls


def _pct(values, p):
    """Nearest-rank percentile of a non-empty list."""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(p * len(ordered)) - 1)]


def report(calls):
    """One row per feature (+ "ALL"): calls, total, per-student stats, per active student-day."""
    rows = []
    for feature in sorted({c[1] for c in calls}) + ["ALL"]:
        mine = [c for c in calls if feature in ("ALL", c[1])]
        per_student, student_days = defaultdict(float), set()
        for day, _, student, usd in mine:
            if student:
                per_student[student] += usd
                student_days.add((student, day))
        spend = list(per_student.values())
        rows.append({
            "feature": feature, "calls": len(mine), "total_usd": sum(c[3] for c in mine),
            "unattributed_usd": sum(c[3] for c in mine if not c[2]), "students": len(spend),
            "avg_per_student_usd": sum(spend) / len(spend) if spend else 0.0,
            "p50_per_student_usd": _pct(spend, 0.5) if spend else 0.0,
            "p90_per_student_usd": _pct(spend, 0.9) if spend else 0.0,
            "max_per_student_usd": max(spend, default=0.0),
            "student_days": len(student_days),
            "per_active_student_day_usd": sum(spend) / len(student_days) if student_days else 0.0,
        })
    for row in rows:  # INR twin of every USD figure
        row.update({k.replace("_usd", "_inr"): v * USD_TO_INR_RATE for k, v in list(row.items()) if k.endswith("_usd")})
    return rows


def _print(rows):
    cols = [("feature", "feature", "{}"), ("calls", "calls", "{}"), ("total", "total", "{:.4f}"),
            ("no student", "unattributed", "{:.4f}"), ("students", "students", "{}"),
            ("avg/stu", "avg_per_student", "{:.4f}"), ("p50/stu", "p50_per_student", "{:.4f}"),
            ("p90/stu", "p90_per_student", "{:.4f}"), ("max/stu", "max_per_student", "{:.4f}"),
            ("stu-days", "student_days", "{}"), ("/stu/day", "per_active_student_day", "{:.4f}")]
    for unit in ("inr", "usd"):
        print(f"\nAI cost in {unit.upper()} (per-student figures count only calls tagged with a student)")
        table = [[h for h, _, _ in cols]]
        for row in rows:
            table.append([fmt.format(row[key] if key in row else row[f"{key}_{unit}"]) for _, key, fmt in cols])
        widths = [max(len(r[i]) for r in table) for i in range(len(cols))]
        for r in table:
            print("  ".join(v.rjust(w) if i else v.ljust(w) for i, (v, w) in enumerate(zip(r, widths))))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("log_file", nargs="?", help="saved log output (default: docker compose logs)")
    ap.add_argument("--since", type=date.fromisoformat, help="first IST day to include, YYYY-MM-DD")
    ap.add_argument("--until", type=date.fromisoformat, help="last IST day to include, YYYY-MM-DD")
    ap.add_argument("--csv", action="store_true", help="print CSV instead of tables")
    args = ap.parse_args(argv)
    if args.log_file:
        lines = Path(args.log_file).read_text(encoding="utf-8", errors="replace").splitlines()
    else:
        lines = subprocess.run(["docker", "compose", "logs", "--no-color", "generation", "colearner"],
                               cwd=Path(__file__).resolve().parent.parent, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", check=True).stdout.splitlines()
    calls = [c for c in _events(lines)
             if (not args.since or c[0] >= args.since) and (not args.until or c[0] <= args.until)]
    if not calls:
        print("No cost lines found.", file=sys.stderr)
        return 1
    rows = report(calls)
    if args.csv:
        writer = csv.DictWriter(sys.stdout, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    else:
        days = sorted({c[0] for c in calls})
        print(f"{len(calls)} AI calls, {days[0]} .. {days[-1]} (IST)")
        _print(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
