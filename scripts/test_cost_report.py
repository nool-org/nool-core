"""python -m pytest scripts/test_cost_report.py - parses testdata/cost_sample.log."""

from datetime import date
from pathlib import Path

import cost_report

SAMPLE = (Path(__file__).parent / "testdata" / "cost_sample.log").read_text().splitlines()


def _rows(calls):
    return {r["feature"]: r for r in cost_report.report(calls)}


def test_every_cost_line_is_parsed_and_realtime_calls_are_joined_to_their_student():
    calls = cost_report._events(SAMPLE)
    assert len(calls) == 7
    assert (date(2026, 10, 11), "voice_test", "stu-b", 0.11) in calls
    assert (date(2026, 10, 11), "voice_test", None, 0.07) in calls  # no start line: unattributed


def test_totals_and_per_student_figures():
    rows = _rows(cost_report._events(SAMPLE))
    assert round(rows["ALL"]["total_usd"], 6) == round(0.002 + 0.006 + 0.05 + 0.0004 + 0.0006 + 0.11 + 0.07, 6)
    assert round(rows["ALL"]["unattributed_usd"], 6) == 0.12  # the teacher's paper + the orphan voice call
    daily = rows["daily_practice_mcq"]
    assert daily["students"] == 2 and daily["max_per_student_usd"] == 0.006 and daily["p50_per_student_usd"] == 0.002
    everyone = rows["ALL"]
    # stu-a: 0.002 + 0.0004 + 0.0006 = 0.003 over 2 days; stu-b: 0.006 + 0.11 = 0.116 over 2 days
    assert everyone["students"] == 2 and everyone["student_days"] == 4
    assert round(everyone["avg_per_student_usd"], 6) == round((0.003 + 0.116) / 2, 6)
    assert round(everyone["per_active_student_day_usd"], 6) == round(0.119 / 4, 6)
    assert round(everyone["total_inr"], 4) == round(everyone["total_usd"] * 86.5, 4)


def test_date_filter_and_csv(capsys, tmp_path):
    log = tmp_path / "x.log"
    log.write_text("\n".join(SAMPLE))
    assert cost_report.main([str(log), "--since", "2026-10-11", "--csv"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("feature,calls,total_usd")
    assert not any(line.startswith("daily_practice_mcq") for line in out)  # 2026-10-10 only
    assert cost_report.main([str(log), "--until", "2026-10-01"]) == 1
