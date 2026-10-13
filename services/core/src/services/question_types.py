"""Question types beyond MCQ and spoken answers - any subject can use them
(the topic quiz mix decides which, topic_quiz.QUIZ_MIX):

- who_is_right: two characters disagree on a claim; the student picks who
  is right, then why. Wrong reasons are tagged with the mistake they show.
- make_your_own: the student builds something that fits ("two numbers with
  HCF 6 and LCM 36"); any right answer counts - answer_checks.py decides.
- fix_steps: "Fix Milo's homework" - a worked solution with one wrong step;
  the student taps it, then picks the right version of it.

Each stores its details in PracticeQuestion.spec. public() is the part the
app gets before an answer (never which choice is right, never the example
answer); grade() marks a response; reveal() is what the app gets after.
Two-part answers score 100 / 50 (right pick, wrong reason or fix) / 0.
"""

import random

from src.services import answer_checks

KINDS = ("who_is_right", "make_your_own", "fix_steps")
CAST = ("Zip", "Bolt")  # who_is_right's two characters (the app draws them); Milo asks


class SpecError(ValueError):
    """A generated spec that doesn't hold together - the question is dropped."""


def validate(kind: str, spec: dict) -> None:
    """Raises SpecError unless the spec is complete and consistent."""
    try:
        if kind == "who_is_right":
            claims, reasons = spec["claims"], spec["reasons"]
            if len(claims) != 2 or not all(c.get("says") for c in claims):
                raise SpecError("two claims needed")
            if spec["correct"] not in (0, 1):
                raise SpecError("correct must be 0 or 1")
            if not 3 <= len(reasons) <= 4 or sum(bool(r.get("correct")) for r in reasons) != 1:
                raise SpecError("3-4 reasons, exactly one correct")
        elif kind == "fix_steps":
            steps, fixes = spec["steps"], spec["fixes"]
            if not 3 <= len(steps) <= 7 or not 0 <= spec["wrong_step"] < len(steps):
                raise SpecError("3-7 steps and a wrong_step among them")
            if not 3 <= len(fixes) <= 4 or sum(bool(f.get("correct")) for f in fixes) != 1:
                raise SpecError("3-4 fixes, exactly one correct")
        elif kind == "make_your_own":
            check = spec["check"]
            if check["kind"] not in answer_checks.KINDS or not answer_checks.valid_params(check["kind"], check["params"]):
                raise SpecError("unknown check or bad params")
            fields = answer_checks.KINDS[check["kind"]].fields
            if len(spec.get("labels") or []) != fields:
                raise SpecError(f"{fields} labels needed")
            ok, message = answer_checks.check(check["kind"], check["params"], [str(x) for x in spec["example"]])
            if not ok:  # a question whose own example fails can't be trusted
                raise SpecError(f"example answer fails its check: {message}")
        else:
            raise SpecError(f"unknown kind {kind}")
    except (KeyError, TypeError) as exc:
        raise SpecError(f"incomplete spec: {exc}") from exc


def shuffle(kind: str, spec: dict, seed: str) -> dict:
    """The spec with its answer moved around, the same way for the same seed: a
    model tends to put the right reason/fix first and make the second
    character right, which students would learn to pick blind. Zip stays the
    first speaker; who is right moves between them."""
    rng = random.Random(seed)
    out = {**spec}
    if kind == "who_is_right":
        claims = [dict(c) for c in spec["claims"]]
        if rng.random() < 0.5:  # swap what they say, keep who speaks first
            claims[0]["says"], claims[1]["says"] = claims[1]["says"], claims[0]["says"]
            out["correct"] = 1 - spec["correct"]
        out["claims"] = claims
        out["reasons"] = rng.sample(list(spec["reasons"]), len(spec["reasons"]))
    elif kind == "fix_steps":
        out["fixes"] = rng.sample(list(spec["fixes"]), len(spec["fixes"]))
    return out


def public(kind: str, spec: dict) -> dict:
    """What the app shows before the student answers."""
    if kind == "who_is_right":
        return {"claims": [{"name": c["name"], "says": c["says"]} for c in spec["claims"]],
                "reasons": [r["text"] for r in spec["reasons"]]}
    if kind == "fix_steps":
        return {"problem": spec.get("problem", ""), "steps": list(spec["steps"]),
                "fixes": [f["text"] for f in spec["fixes"]]}
    if kind == "make_your_own":
        return {"labels": list(spec["labels"])}
    return {}


def grade(kind: str, spec: dict, response: dict) -> dict:
    """{score, correct, message, mistake} for a response - malformed ones score 0."""
    try:
        if kind == "who_is_right":
            pick, reason = int(response["pick"]), int(response["reason"])
            right_pick = pick == spec["correct"]
            chosen = spec["reasons"][reason]
            right_reason = bool(chosen.get("correct"))
            score = 100 if right_pick and right_reason else 50 if right_pick else 0
            mistake = None if score == 100 else chosen.get("mistake") if right_pick else "picked_wrong_side"
            message = ("Right - and for the right reason!" if score == 100 else
                       f"{spec['claims'][pick]['name']} is right, but that's not why." if right_pick else
                       f"Not quite - {spec['claims'][spec['correct']]['name']} is right.")
        elif kind == "fix_steps":
            step, fix = int(response["step"]), int(response["fix"])
            found = step == spec["wrong_step"]
            fixed = found and bool(spec["fixes"][fix].get("correct"))
            score = 100 if fixed else 50 if found else 0
            mistake = None if fixed else spec["fixes"][fix].get("mistake") if found else "missed_the_error"
            message = ("You caught Milo's mistake and fixed it!" if fixed else
                       "Good catch - that's the wrong step, but the fix isn't right." if found else
                       f"Milo's mistake is in step {spec['wrong_step'] + 1}.")
        elif kind == "make_your_own":
            values = [str(v) for v in response["values"]]
            ok, message = answer_checks.check(spec["check"]["kind"], spec["check"]["params"], values)
            score, mistake = (100, None) if ok else (0, "make_your_own")
        else:
            raise KeyError(kind)
    except (KeyError, TypeError, ValueError, IndexError):
        return {"score": 0, "correct": False, "message": "We couldn't read that answer.", "mistake": None}
    return {"score": score, "correct": score == 100, "message": message, "mistake": mistake}


def reveal(kind: str, spec: dict) -> dict:
    """The answer, shown once the student has answered."""
    if kind == "who_is_right":
        return {"correct": spec["correct"], "reason": next(i for i, r in enumerate(spec["reasons"]) if r.get("correct")),
                "explanation": spec.get("explanation", "")}
    if kind == "fix_steps":
        return {"wrong_step": spec["wrong_step"],
                "fix": next(i for i, f in enumerate(spec["fixes"]) if f.get("correct")),
                "explanation": spec.get("explanation", "")}
    if kind == "make_your_own":
        return {"example": [str(x) for x in spec["example"]], "explanation": spec.get("explanation", "")}
    return {}
