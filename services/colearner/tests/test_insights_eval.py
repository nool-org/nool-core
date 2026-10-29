"""AI-021: the insights eval's checkers and fact sets, on canned outputs (no API).
Skipped when evals/ is not next to tests/."""
import importlib.util
import json
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "evals" / "insights_eval.py"
if not _PATH.is_file():
    pytest.skip("evals/ not available in this environment", allow_module_level=True)
_spec = importlib.util.spec_from_file_location("insights_eval", _PATH)
ev = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ev)

FACTS = {"chapter": "Electricity", "mastery_percent": 55, "topics": [{"name": "Ohm's Law", "percent": 40}],
         "cards": [{"question_number": 2, "expected_points": ["V = IR", "R is 10 ohm"], "topic": "Ohm's Law",
                    "subtopic": "Resistance"}]}
GOOD = {"headline": "Strong at Remember, still growing at Analyze.", "recap_text": "You did well today, keep going.",
        "next_steps": [{"title": "Redo the V = IR sums", "reference": "Ohm's Law"},
                       {"title": "Try resistance with 10 ohm", "reference": "Electricity, Resistance"},
                       {"title": "Revise", "reference": "Electricity"}]}


def test_a_clean_recap_passes_all_three_checks():
    assert ev.check(GOOD, FACTS) == {"numbers_in_recap": [], "new_numbers": [], "unnamed_references": []}


def test_a_number_in_the_recap_is_found_as_a_digit_or_a_word():
    words = {**GOOD, "recap_text": "You scored 55 percent, nearly Half, and two steps remain. One step at a time."}
    assert ev.numbers_said(words["recap_text"]) == ["55", "percent", "Half", "two"] or \
        ev.numbers_said(words["recap_text"]) == ["55", "percent", "half", "two"]


def test_numbers_not_in_the_facts_are_flagged_but_given_ones_are_not():
    words = {**GOOD, "headline": "A strong 90 at Remember",
             "next_steps": [{"title": "Do 3 sums with 10 ohm", "reference": "Ohm's Law"}] * 3}
    assert ev.new_numbers(words, FACTS) == ["90", "3", "3", "3"]


def test_a_reference_must_name_a_topic_subtopic_or_the_chapter_from_the_facts():
    words = {**GOOD, "next_steps": [{"title": "t", "reference": "Magnetic Effects, section 13"},
                                    {"title": "t", "reference": "NCERT Class 10, Resistance"},
                                    {"title": "t", "reference": "Page 4 of the textbook"}]}
    assert ev.unnamed_references(words, FACTS) == ["Magnetic Effects, section 13", "Page 4 of the textbook"]


def test_summary_counts_per_rule_and_skips_errors():
    bad = {"error": False, "checks": ev.check({**GOOD, "recap_text": "You got 5."}, FACTS)}
    ok = {"error": False, "checks": ev.check(GOOD, FACTS)}
    out = ev.summarize([{**bad, "seconds": 4.0}, {**ok, "seconds": 8.0}, {"error": True, "checks": {}, "seconds": 20.0}])
    assert out == {"recaps": 3, "errors": 1, "seconds_median": 8.0, "seconds_p90": 8.0,
                   "timeouts_at_production_limit": 1, "recap_says_a_number": 1,
                   "invented_number_in_headline_or_steps": 0, "reference_names_no_given_topic": 0}


def test_twenty_fact_sets_are_built_from_the_grader_sessions_and_the_edge_cases():
    data = json.loads(_PATH.with_name("grader_set.json").read_text(encoding="utf-8"))
    sets = [ev.facts_from_session(s) for s in data["sessions"]] + ev.handwritten()
    assert len(sets) >= 20
    missed = next(f for f in sets if f["chapter"] == "Light - Reflection and Refraction")
    assert missed["cards"] and missed["cards"][0]["missed_points"] and missed["topics"][0]["name"] == missed["chapter"]
    assert any(not f["cards"] for f in sets)  # a session with nothing missed
