"""who_is_right / make_your_own / fix_steps: specs that don't hold together
are rejected, the app never sees which choice is right before answering,
and two-part answers score 100 / 50 / 0. Pure - no database."""

import pytest

from src.services import question_types as qt

WHO = {"claims": [{"name": "Zip", "says": "Yes, it gets huge."}, {"name": "Bolt", "says": "No, never."}],
       "correct": 1, "explanation": "6ⁿ = 2ⁿ × 3ⁿ has no 5.",
       "reasons": [{"text": "6 is even", "correct": False, "mistake": "even_means_divisible"},
                   {"text": "6ⁿ has no factor 5", "correct": True, "mistake": ""},
                   {"text": "Powers of 6 end in 6", "correct": False, "mistake": "pattern_not_proof"}]}
FIX = {"problem": "HCF of 12 and 18", "steps": ["12 = 2² × 3", "18 = 2 × 3²", "HCF = 2² × 3² = 36"],
       "wrong_step": 2, "explanation": "HCF takes the smallest powers.",
       "fixes": [{"text": "HCF = 2 × 3 = 6", "correct": True, "mistake": ""},
                 {"text": "HCF = 2² × 3 = 12", "correct": False, "mistake": "mixed_powers"},
                 {"text": "HCF = 12 × 18", "correct": False, "mistake": "product_not_hcf"}]}
MINE = {"check": {"kind": "pair_hcf_lcm", "params": {"hcf": 6, "lcm": 36}}, "labels": ["First", "Second"],
        "example": ["12", "18"], "explanation": "Both are multiples of 6 and divide 36."}


def test_valid_specs_pass_and_broken_ones_are_rejected():
    for kind, spec in (("who_is_right", WHO), ("fix_steps", FIX), ("make_your_own", MINE)):
        qt.validate(kind, spec)
    broken = [
        ("who_is_right", {**WHO, "correct": 2}),
        ("who_is_right", {**WHO, "reasons": [{**r, "correct": True} for r in WHO["reasons"]]}),
        ("fix_steps", {**FIX, "wrong_step": 9}),
        ("make_your_own", {**MINE, "example": ["6", "30"]}),  # its own example fails its check
        ("make_your_own", {**MINE, "check": {"kind": "run_my_code", "params": {}}}),
        ("make_your_own", {**MINE, "labels": ["Only one"]}),
        ("who_is_right", {"claims": []}),
    ]
    for kind, spec in broken:
        with pytest.raises(qt.SpecError):
            qt.validate(kind, spec)


def test_public_never_reveals_the_answer():
    for kind, spec in (("who_is_right", WHO), ("fix_steps", FIX), ("make_your_own", MINE)):
        shown = str(qt.public(kind, spec))
        assert "correct" not in shown and "wrong_step" not in shown and "example" not in shown
        assert "12', '18'" not in shown
    assert qt.public("who_is_right", WHO)["reasons"] == ["6 is even", "6ⁿ has no factor 5", "Powers of 6 end in 6"]


def test_two_part_answers_score_full_half_or_none():
    assert qt.grade("who_is_right", WHO, {"pick": 1, "reason": 1})["score"] == 100
    half = qt.grade("who_is_right", WHO, {"pick": 1, "reason": 2})
    assert half["score"] == 50 and half["mistake"] == "pattern_not_proof" and not half["correct"]
    assert qt.grade("who_is_right", WHO, {"pick": 0, "reason": 1})["mistake"] == "picked_wrong_side"
    assert qt.grade("fix_steps", FIX, {"step": 2, "fix": 0})["score"] == 100
    assert qt.grade("fix_steps", FIX, {"step": 2, "fix": 1})["mistake"] == "mixed_powers"
    assert qt.grade("fix_steps", FIX, {"step": 0, "fix": 0})["message"] == "Milo's mistake is in step 3."
    assert qt.grade("make_your_own", MINE, {"values": ["36", "6"]})["correct"]
    assert qt.grade("make_your_own", MINE, {"values": ["6", "30"]})["score"] == 0
    assert qt.grade("who_is_right", WHO, {"pick": "x"})["score"] == 0  # malformed


def test_reveal_after_answering():
    assert qt.reveal("who_is_right", WHO)["correct"] == 1 and qt.reveal("who_is_right", WHO)["reason"] == 1
    assert qt.reveal("fix_steps", FIX) == {"wrong_step": 2, "fix": 0, "explanation": FIX["explanation"]}
    assert qt.reveal("make_your_own", MINE)["example"] == ["12", "18"]


def test_shuffle_moves_the_answer_but_keeps_it_right():
    sides, first_reason, first_fix = set(), set(), set()
    for n in range(40):
        who = qt.shuffle("who_is_right", WHO, f"seed-{n}")
        qt.validate("who_is_right", who)
        assert [c["name"] for c in who["claims"]] == ["Zip", "Bolt"]  # Zip still speaks first
        right = who["claims"][who["correct"]]["says"]
        assert right == WHO["claims"][WHO["correct"]]["says"]  # the right words stay the right answer
        sides.add(who["correct"])
        first_reason.add(who["reasons"][0]["correct"])
        fix = qt.shuffle("fix_steps", FIX, f"seed-{n}")
        qt.validate("fix_steps", fix)
        first_fix.add(fix["fixes"][0]["correct"])
    assert sides == {0, 1} and first_reason == {True, False} and first_fix == {True, False}
    assert qt.shuffle("who_is_right", WHO, "x") == qt.shuffle("who_is_right", WHO, "x")  # same seed, same order
