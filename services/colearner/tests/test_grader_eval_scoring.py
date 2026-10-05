"""Scoring arithmetic of the grader accuracy gate (evals/grader_eval.py). No API calls."""
import importlib.util
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "evals" / "grader_eval.py"
if not _PATH.exists():
    pytest.skip("evals/ not mounted next to tests/", allow_module_level=True)
_spec = importlib.util.spec_from_file_location("grader_eval", _PATH)
ge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ge)

SESSION = {"id": "s", "turns": [
    {"role": "co_learner", "text": "Are you ready?"},
    {"role": "student", "text": "Yes, I'm ready."},
    {"role": "co_learner", "text": "What is current?"},
    {"role": "student", "text": "Current is the flow of electric charge through a wire", "label": "correct"},
    {"role": "co_learner", "text": "Double the voltage?"},
    {"role": "student", "text": "The current stays the same because resistance is fixed", "label": "wrong"},
    {"role": "co_learner", "text": "Which way does current flow?"},
    {"role": "student", "text": "From negative to positive terminal", "label": "ambiguous"},
]}


def _qa(resp, correct, score):
    return {"student_response": resp, "is_substantively_correct": correct, "score_percent": score}


def test_align_matches_by_overlap_and_flags_skipped_answers():
    report = {"qa_transcripts": [
        _qa("the current stays the same because the resistance is fixed", True, 70),  # false pass
        _qa("current is flow of electric charge through wire", True, 90),
        _qa("completely unrelated words here", False, 0),  # matches nothing
    ], "session_summary": {"overall_mastery_percent": 60}}
    rows = ge.align(SESSION, report)
    assert [(r["label"], r["verdict"], r["score"]) for r in rows] == [
        ("correct", True, 90), ("wrong", True, 70), ("ambiguous", None, None)]
    assert ge.mastery_gap(report, rows) == 20


def test_summary_excludes_ambiguous_and_applies_the_gate():
    rows = ([{"label": "correct", "verdict": True}] * 28 + [{"label": "correct", "verdict": None}]  # skip = miss
            + [{"label": "wrong", "verdict": False}] * 30 + [{"label": "ambiguous", "verdict": True}] * 5)
    s = ge.summarize(rows, [2, 4], 0.1234567, 0)
    assert s["answers_scored"] == 59 and s["skipped_by_grader"] == 1
    assert s["verdict_accuracy_pct"] == 98.3
    assert s["wrong_marked_correct"] == "0/30" and s["right_marked_wrong"] == "0/29"
    assert s["mastery_gap_mean"] == 3 and s["cost_usd"] == 0.1235 and s["passed"]

    one_pass = rows[:-5 - 30] + [{"label": "wrong", "verdict": True}] + [{"label": "wrong", "verdict": False}] * 29
    assert ge.summarize(one_pass, [], 0, 0)["passed"]  # exactly 1 in 30 is allowed
    two_pass = one_pass[:-1] + [{"label": "wrong", "verdict": True}]
    assert not ge.summarize(two_pass, [], 0, 0)["passed"]

    low_acc = [{"label": "correct", "verdict": False}] * 4 + [{"label": "correct", "verdict": True}] * 46
    assert ge.summarize(low_acc, [], 0, 0)["verdict_accuracy_pct"] == 92.0
    assert not ge.summarize(low_acc, [], 0, 0)["passed"]
