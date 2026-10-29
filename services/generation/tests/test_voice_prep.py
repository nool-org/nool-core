"""Voice-test prep workflow with a scripted fake LLM - no network."""

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from src.services.voice_prep import (
    PrepRequest,
    _points,
    _problems,
    _Rewording,
    _Verdict,
    _Verdicts,
    _Version,
    prep_questions,
    spoken,
)

Q_APPLY = {"id": "q-apply", "text": "A 220 V supply drives a current of 5 A through a heater. Find its resistance.",
           "answer": "R = V/I = 44 Ω", "bloom_level": "APPLY", "topic_id": "t1", "subtopic_id": "s1",
           "concepts": ["Ohm's law"], "difficulty": "MEDIUM"}
Q_REMEMBER = {"id": "q-rem", "text": "Define resistivity.", "answer": "Resistance of a 1 m long wire of 1 m² area.",
              "bloom_level": "REMEMBER", "difficulty": "EASY"}
Q_EXTRA = {"id": "q-extra", "text": "State Ohm's law.", "answer": "V is proportional to I.", "bloom_level": "REMEMBER"}

REQUEST = PrepRequest(grade=10, subject="Science", chapter="Electricity", board="CBSE",
                      levels=["APPLY", "REMEMBER"], candidates=[Q_REMEMBER, Q_EXTRA, Q_APPLY])


def version(text, points=("e1", "e2")):
    return _Version(reworded_text=text, spoken_text=text, expected_points=list(points),
                    misconceptions=["m1", "m2"], hints=["key idea?", "smaller question?"])


EASIER = version("A heater takes 5 A from a 220 V plug point. Which law links these to its resistance?")


def rewording(text, **kw):
    return _Rewording(reworded_text=text, spoken_text=kw.get("spoken", text), expected_points=["p1", "p2"],
                      misconceptions=["m1", "m2"], hints=["gentle?", "stronger?"], easier=kw.get("easier", EASIER))


GOOD_APPLY = rewording("A heater is connected to a 220 V plug point. The current is 5 A. What is its resistance?")
GOOD_REMEMBER = rewording("What is resistivity? Tell me in your own words.")


class FakeLLM:
    """Replays scripted outputs per output type and records every call."""

    def __init__(self, drafts, verdicts):
        self.script = {_Rewording: list(drafts), _Verdicts: list(verdicts)}
        self.calls = []

    async def __call__(self, *, model, effort, system, user, output_type):
        self.calls.append((output_type.__name__, model, user))
        return self.script[output_type].pop(0)


def ok(*idx):
    return [_Verdict(index=i, answer_to_reworded="a", ok=True, problem="", easier_ok=True) for i in idx]


@pytest.mark.asyncio
async def test_happy_path_one_question_per_level_in_order_with_rubric():
    llm = FakeLLM([GOOD_APPLY, GOOD_REMEMBER], [_Verdicts(verdicts=ok(0, 1))])
    result = await prep_questions(llm, REQUEST, generation_model="gen", verify_model="ver")
    qs = result.questions
    # levels order, not candidate order; the extra REMEMBER candidate is ignored
    assert [(q.order, q.bloom_level, q.dataset_question_id) for q in qs] == [(1, "APPLY", "q-apply"), (2, "REMEMBER", "q-rem")]
    assert qs[0].reworded_text == GOOD_APPLY.reworded_text and qs[0].original_text == Q_APPLY["text"]
    assert (qs[0].topic_id, qs[0].subtopic_id, qs[0].concepts) == ("t1", "s1", ["Ohm's law"])
    assert qs[0].hints == ["gentle?", "stronger?"] and len(qs[0].expected_points) == 2
    assert [c[:2] for c in llm.calls] == [("_Rewording", "gen"), ("_Rewording", "gen"), ("_Verdicts", "ver")]
    assert '"dataset_answer": "R = V/I = 44 Ω"' in llm.calls[2][2]


@pytest.mark.asyncio
async def test_verify_failure_regenerates_once_then_falls_back_to_original_wording_keeping_the_rubric():
    bad = _Verdict(index=0, answer_to_reworded="x", ok=False, problem="answer changed", easier_ok=False)
    llm = FakeLLM([GOOD_APPLY, GOOD_REMEMBER, GOOD_APPLY],
                  [_Verdicts(verdicts=[bad] + ok(1)), _Verdicts(verdicts=[bad])])
    result = await prep_questions(llm, REQUEST, generation_model="gen", verify_model="ver")
    first = result.questions[0]
    assert first.reworded_text == first.original_text == Q_APPLY["text"]
    assert first.spoken_text == Q_APPLY["text"]       # no symbols to say in words here
    assert first.hints == ["gentle?", "stronger?"]      # rubric from the last draft still filled
    assert result.questions[1].reworded_text == GOOD_REMEMBER.reworded_text
    assert '"feedback": "answer changed"' in llm.calls[3][2] and "Define resistivity" not in llm.calls[3][2]
    assert len(llm.calls) == 5


@pytest.mark.asyncio
async def test_a_regenerated_item_that_passes_is_used():
    bad = _Verdict(index=1, answer_to_reworded="x", ok=False, problem="too easy now", easier_ok=False)
    fixed = rewording("What do we mean by resistivity?")
    llm = FakeLLM([GOOD_APPLY, GOOD_REMEMBER, fixed], [_Verdicts(verdicts=ok(0) + [bad]), _Verdicts(verdicts=ok(1))])
    result = await prep_questions(llm, REQUEST, generation_model="gen", verify_model="ver")
    assert result.questions[1].reworded_text == "What do we mean by resistivity?"


@pytest.mark.asyncio
async def test_a_dropped_number_or_unit_is_rejected_in_code_and_fed_back():
    lost_unit = rewording("A heater runs on 220 V. The current is 5. What is its resistance?")
    llm = FakeLLM([lost_unit, GOOD_REMEMBER, GOOD_APPLY], [_Verdicts(verdicts=ok(1)), _Verdicts(verdicts=ok(0))])
    result = await prep_questions(llm, REQUEST, generation_model="gen", verify_model="ver")
    assert result.questions[0].reworded_text == GOOD_APPLY.reworded_text
    assert '"index": 0' not in llm.calls[2][2]          # never sent to the verifier
    assert "missing: 5 A" in llm.calls[3][2]              # the regenerate prompt says what was lost


def test_code_checks():
    orig = Q_APPLY["text"]
    assert _problems(orig, GOOD_APPLY) is None
    assert "not in the original: 230" in _problems(orig, rewording("A heater on 230 V takes 5 A. What is its resistance?"))
    assert "longer than" in _problems(orig, rewording(
        "A heater that is connected to a 220 V supply in the kitchen of a house draws a current of 5 A so find R."))
    assert "avoid: gonna" in _problems(orig, rewording("A heater on 220 V takes 5 A. What is it gonna be?"))
    assert "hints" in _problems(orig, GOOD_APPLY.model_copy(update={"hints": ["only one"]}))
    assert _problems("Two resistors of 2 Ω each.", rewording("Two resistors, each 2 Ω.")) is None
    assert "missing: 2 Ω" in _problems("Two resistors of 2 Ω each.", rewording("Two resistors, each 2 ohm."))


def test_spoken_fallback_says_symbols_in_words():
    assert spoken("V = IR, R = 2 Ω, T = 30 °C") == "V equals IR, R equals 2 ohm, T equals 30 degree Celsius"


def test_request_limits_and_missing_level():
    with pytest.raises(ValidationError):
        PrepRequest(grade=10, subject="S", chapter="C", levels=["APPLY"], candidates=[Q_APPLY] * 13)
    with pytest.raises(ValidationError, match="no candidate left for Bloom level EVALUATE"):
        PrepRequest(grade=10, subject="S", chapter="C", levels=["EVALUATE"], candidates=[Q_APPLY])
    with pytest.raises(ValidationError):
        PrepRequest(grade=10, subject="S", chapter="C", levels=["APPLY"], candidates=[{**Q_APPLY, "text": "x" * 4001}])


def test_api_answers_503_without_a_key_and_runs_with_one(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    from src.main import create_app

    app = create_app()
    with TestClient(app) as client:
        body = REQUEST.model_dump()
        assert client.post("/v1/voice-test/prep", json=body).status_code == 503
        assert client.post("/v1/voice-test/prep", json={**body, "levels": ["CREATE"]}).status_code == 422
        app.state.llm = FakeLLM([GOOD_APPLY, GOOD_REMEMBER], [_Verdicts(verdicts=ok(0, 1))])
        response = client.post("/v1/voice-test/prep", json=body)
        assert response.status_code == 200
        assert [q["dataset_question_id"] for q in response.json()["questions"]] == ["q-apply", "q-rem"]


def test_words_after_numbers_are_not_units():
    assert _problems("If 5 and 7 are added, what do you get?", rewording("What do we get when we add 5 and 7?")) is None


def test_marks_at_the_end_need_not_be_kept():
    assert _problems("Which process is better and why? (2)", rewording("Which method is better and why?")) is None


def test_a_line_break_ends_a_sentence():
    orig = Q_APPLY["text"]
    two_lines = "A heater on a plug point runs on 220 V and takes 5 A of current\nWhat is its resistance in ohms?"
    assert _problems(orig, rewording(two_lines)) is None


LENS = ("A convex lens has focal length 20 cm. An object is kept 10 cm in front of its optical centre. "
        "In this case, write any four characteristics of the image formed by this lens.")


def test_written_asks_are_rejected_but_a_narrowed_spoken_ask_passes():
    write_four = rewording("A convex lens has focal length 20 cm. An object is 10 cm from it. "
                           "Write any four features of the image.")
    assert "'Write'" in _problems(LENS, write_four)
    assert "'any four'" in _problems(LENS, rewording("A convex lens has focal length 20 cm. The object is 10 cm away. "
                                                     "Tell me any four features of the image."))
    assert "'draw'" in _problems(LENS, rewording("Think of a convex lens. Can you draw the image?"))
    assert "'tabulate'" in _problems(LENS, rewording("Show the two kinds side by side.", spoken="Please tabulate it."))
    narrowed = rewording("A convex lens has focal length 20 cm. An object is kept 10 cm from the lens. "
                         "Tell me two or three things about the image it forms.")
    assert _problems(LENS, narrowed) is None
    # a dropped number is fine when the ask narrows; the verifier checks it's still answerable
    assert _problems(LENS, rewording("An object is kept between a convex lens and its focus. "
                                     "Tell me two or three things about the image.")) is None
    assert "not in the original: 2, 3" in _problems(LENS, rewording("A convex lens has focal length 20 cm. "
                                                                     "Tell me 2 or 3 features of the image."))


def test_prompts_carry_the_voice_first_rules():
    from src.services.voice_prep import GENERATE_SYSTEM, VERIFY_SYSTEM
    prompt = GENERATE_SYSTEM.format(board="CBSE", grade=10, subject="Science")
    assert "answered by speaking" in prompt and "Tell me two or three" in prompt and "this lens" in prompt
    assert "do not add or drop any part" not in prompt
    verify = VERIFY_SYSTEM.format(board="CBSE", grade=10, subject="Science")
    assert "fewer items" in verify and "Self-contained" in verify and "Answerable by speaking" in verify


@pytest.mark.asyncio
async def test_every_question_carries_its_easier_version_with_its_own_rubric():
    llm = FakeLLM([GOOD_APPLY, GOOD_REMEMBER.model_copy(update={"easier": version("What does resistivity depend on?")})],
                  [_Verdicts(verdicts=ok(0, 1))])
    result = await prep_questions(llm, REQUEST, generation_model="gen", verify_model="ver")
    easier = result.questions[0].easier
    assert easier.spoken_text == EASIER.spoken_text and easier.expected_points == ["e1", "e2"]
    assert easier.hints == ["key idea?", "smaller question?"] and easier.misconceptions == ["m1", "m2"]
    assert result.questions[1].easier.spoken_text == "What does resistivity depend on?"
    assert '"easier": {' in llm.calls[2][2]  # the verifier sees it


@pytest.mark.asyncio
async def test_an_easier_version_failing_code_checks_or_the_verifier_is_null_but_the_question_stays():
    adds_a_number = version("Define resistivity for a 3 m wire.")
    rejected = [_Verdict(index=0, answer_to_reworded="a", ok=True, problem="", easier_ok=False)] + ok(1)
    llm = FakeLLM([GOOD_APPLY, GOOD_REMEMBER.model_copy(update={"easier": adds_a_number})], [_Verdicts(verdicts=rejected)])
    result = await prep_questions(llm, REQUEST, generation_model="gen", verify_model="ver")
    assert [q.reworded_text for q in result.questions] == [GOOD_APPLY.reworded_text, GOOD_REMEMBER.reworded_text]
    assert [q.easier for q in result.questions] == [None, None]
    assert '"easier": null' in llm.calls[2][2]  # code-rejected easier never reaches the verifier
    assert len(llm.calls) == 3                   # and causes no retry


def test_code_checks_apply_to_the_easier_version():
    from src.services.voice_prep import _without_bad_easier
    orig = Q_APPLY["text"]
    assert _without_bad_easier(orig, GOOD_APPLY).easier == EASIER
    for bad in (version("A heater takes 5 A on 230 V. What is its resistance?"),
                version("Write the law that links 220 V and 5 A."),
                version("What is its resistance?", points=("a", "b", "c", "d")),
                EASIER.model_copy(update={"hints": ["only one"]})):
        assert _without_bad_easier(orig, GOOD_APPLY.model_copy(update={"easier": bad})).easier is None


def test_prompts_ask_for_a_nudge_ladder_and_an_easier_version():
    from src.services.voice_prep import GENERATE_SYSTEM, VERIFY_SYSTEM
    prompt = GENERATE_SYSTEM.format(board="CBSE", grade=10, subject="Science")
    assert "Hint 1 points the student to the key concept" in prompt and "smaller, easier sub-question" in prompt
    assert "one notch more supported" in prompt and "one Bloom level lower" in prompt
    assert "If `instruction` is given" in prompt
    verify = VERIFY_SYSTEM.format(board="CBSE", grade=10, subject="Science")
    assert "easier_ok=true only if" in verify


@pytest.mark.asyncio
async def test_an_instruction_reaches_the_generator():
    request = PrepRequest(grade=10, subject="Science", chapter="Electricity", levels=["APPLY"],
                          candidates=[{**Q_APPLY, "instruction": "Make it clearer to hear."}])
    llm = FakeLLM([GOOD_APPLY], [_Verdicts(verdicts=ok(0))])
    await prep_questions(llm, request, generation_model="gen", verify_model="ver")
    assert '"instruction": "Make it clearer to hear."' in llm.calls[0][2]


def test_one_short_question_and_hints_that_only_ask():
    orig = "Explain how a concave mirror and a convex mirror reflect parallel rays."
    two = rewording("How does a concave mirror reflect parallel rays? And how does a convex mirror?")
    assert "ONE question" in _problems(orig, two)
    long = rewording(" ".join(["Think"] * 16) + ". " + " ".join(["about"] * 16) + " mirrors?")
    assert "about 20 words" in _problems(orig, long)
    points = ["For a concave mirror the parallel rays meet at the focus after reflection", "p2"]
    leak = rewording("Where do parallel rays go after a concave mirror?").model_copy(update={"expected_points": points, "hints": [
        "What do you know about curved mirrors?", "Do the parallel rays meet at the focus after reflection?"]})
    assert "gives away" in _problems(orig, leak)
    told = rewording("Where do parallel rays go after a convex mirror?").model_copy(update={"hints": ["Rays spread out from a convex mirror.", "Where do they go?"]})
    assert "never a statement" in _problems(orig, told)


def test_marking_notes_never_reach_expected_points():
    """S-004/C-004: "Any two or three of these are enough." was stored as a point and shown as a missed idea."""
    assert _points(["Iron rusts.", "Paint stops air. Any two or three of these are enough.",
                    "Any two of these are enough.", "Any one of these is enough"]) == ["Iron rusts.", "Paint stops air."]
    draft = version("Why does iron rust?", points=["Iron reacts with air.", "Any two or three of these are enough."])
    assert "expected_points" in _problems("Why does iron rust?", draft)  # one real point, not two
    from src.services.voice_prep import GENERATE_SYSTEM
    assert "Any two or three of these are enough" not in GENERATE_SYSTEM


def test_prompts_give_one_word_limit_and_number_checks_in_order():
    """AI-016: one length rule matching MAX_QUESTION_WORDS, verifier checks 1-8 then E, equations banned."""
    import re

    from src.services import voice_prep as vp

    gen = vp.GENERATE_SYSTEM.format(board="CBSE", grade=10, subject="Science")
    assert "35 words" not in gen and f"never more than {vp.MAX_QUESTION_WORDS}" in gen and "equation" in gen
    verify = vp.VERIFY_SYSTEM.format(board="CBSE", grade=10, subject="Science")
    assert re.findall(r"^(\d+|E)\. ", verify, re.M) == [*map(str, range(1, 9)), "E"]
    assert "check E" in vp._Verdict.model_fields["easier_ok"].description
