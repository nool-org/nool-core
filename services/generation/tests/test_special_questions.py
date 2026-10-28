import asyncio

from src.services import special_questions as svc


class FakeLLM:
    """Answers each call from a queue keyed by output type."""

    def __init__(self, replies):
        self.replies = {k: list(v) for k, v in replies.items()}
        self.calls = []

    async def __call__(self, *, model, effort, system, user, output_type):
        self.calls.append({"model": model, "type": output_type.__name__, "system": system, "user": user})
        return output_type.model_validate(self.replies[output_type.__name__].pop(0))


WHO = {"question": "Can 6ⁿ end with 0?", "correct": 1, "explanation": "No 5 in 6ⁿ.",
       "claims": [{"name": "Riya", "says": "Yes, it gets big."}, {"name": "Arjun", "says": "No, never."}],
       "reasons": [{"text": "6ⁿ = 2ⁿ × 3ⁿ has no 5", "correct": True, "mistake": ""},
                   {"text": "6 is even", "correct": False, "mistake": "even_means_divisible"},
                   {"text": "Powers of 6 end in 6", "correct": False, "mistake": "pattern_not_proof"}]}
MINE = {"question": "Give two numbers whose HCF is 6 and LCM is 36.", "check_kind": "pair_hcf_lcm",
        "params_json": '{"hcf": 6, "lcm": 36}', "labels": ["First", "Second"], "example": ["12", "18"],
        "explanation": "Both multiples of 6."}


def request(*kinds):
    return svc.SpecialRequest(grade=10, subject="Mathematics", chapter="Real Numbers", topic="1.2 FTA",
                              context="Every composite number is a product of primes...",
                              slots=[{"kind": k, "bloom_level": "ANALYZE"} for k in kinds],
                              checks=[{"kind": "pair_hcf_lcm", "describe": "two numbers with given HCF and LCM"}])


def test_writes_verifies_and_maps_to_cores_spec():
    llm = FakeLLM({"WhoIsRight": [WHO], "Verdict": [{"ok": True, "problem": ""}] * 2, "MakeYourOwn": [MINE]})
    out = asyncio.run(svc.write_questions(llm, request("who_is_right", "make_your_own"), model="gpt-5.1",
                                          verify_model="gpt-5-mini")).questions
    who, mine = sorted(out, key=lambda q: q.slot_index)
    assert who.text == "Can 6ⁿ end with 0?" and [c["name"] for c in who.spec["claims"]] == ["Zip", "Bolt"]
    assert who.spec["correct"] == 1 and who.spec["reasons"][1]["mistake"] == "even_means_divisible"
    assert mine.spec["check"] == {"kind": "pair_hcf_lcm", "params": {"hcf": 6, "lcm": 36}}
    assert mine.spec["example"] == ["12", "18"]
    types = [c["type"] for c in llm.calls]
    assert types.count("Verdict") == 2  # make_your_own too: do its params state the question's condition?
    assert "pair_hcf_lcm: two numbers" in next(c for c in llm.calls if c["type"] == "MakeYourOwn")["system"]


def test_a_rejected_draft_is_rewritten_once_then_dropped():
    bad = {"ok": False, "problem": "Both reasons are right."}
    llm = FakeLLM({"WhoIsRight": [WHO, WHO], "Verdict": [bad, {"ok": True, "problem": ""}]})
    assert len(asyncio.run(svc.write_questions(llm, request("who_is_right"), model="m", verify_model="v")).questions) == 1
    assert "Both reasons are right." in llm.calls[2]["user"]

    llm = FakeLLM({"WhoIsRight": [WHO, WHO], "Verdict": [bad, bad]})
    assert asyncio.run(svc.write_questions(llm, request("who_is_right"), model="m", verify_model="v")).questions == []


def test_a_question_built_from_an_official_one_uses_it_and_the_cheaper_model():
    llm = FakeLLM({"WhoIsRight": [WHO], "Verdict": [{"ok": True, "problem": ""}]})
    body = request("who_is_right")
    body.slots[0].source = svc.Source(question="Can 15ⁿ end with the digit 0? Give reasons.", answer="No",
                                      solution="15ⁿ = 3ⁿ × 5ⁿ has no factor 2.")
    [q] = asyncio.run(svc.write_questions(llm, body, model="gpt-5.1", verify_model="gpt-5-mini")).questions
    writer = next(c for c in llm.calls if c["type"] == "WhoIsRight")
    assert writer["model"] == "gpt-5-mini" and "OFFICIAL QUESTION: Can 15ⁿ end with the digit 0?" in writer["system"]
    assert "15ⁿ = 3ⁿ × 5ⁿ has no factor 2." in writer["system"] and q.kind == "who_is_right"


def test_a_make_your_own_with_params_not_matching_its_question_is_rewritten():
    """AI-019: make_your_own skipped the verifier, so params of another condition (or unreadable
    params_json kept as {}) would mark right answers wrong."""
    off = {**MINE, "params_json": '{"hcf": 6, "lcm": 72}', "example": ["18", "24"]}
    llm = FakeLLM({"MakeYourOwn": [off, MINE], "Verdict": [{"ok": False, "problem": "LCM 72, not 36."},
                                                            {"ok": True, "problem": ""}]})
    [q] = asyncio.run(svc.write_questions(llm, request("make_your_own"), model="m", verify_model="v")).questions
    assert q.spec["check"]["params"] == {"hcf": 6, "lcm": 36} and "LCM 72, not 36." in llm.calls[2]["user"]
    assert "say exactly the condition" in llm.calls[1]["system"]

    llm = FakeLLM({"MakeYourOwn": [{**MINE, "params_json": "hcf 6"}] * 2, "Verdict": []})
    assert asyncio.run(svc.write_questions(llm, request("make_your_own"), model="m", verify_model="v")).questions == []
