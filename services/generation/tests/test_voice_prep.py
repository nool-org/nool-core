"""Voice-test prep workflow with a scripted fake LLM - no network."""

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from src.services.voice_prep import PrepRequest, _problems, _Rewording, _Verdict, _Verdicts, prep_questions, spoken

Q_APPLY = {"id": "q-apply", "text": "A 220 V supply drives a current of 5 A through a heater. Find its resistance.",
           "answer": "R = V/I = 44 Ω", "bloom_level": "APPLY", "topic_id": "t1", "subtopic_id": "s1",
           "concepts": ["Ohm's law"], "difficulty": "MEDIUM"}
Q_REMEMBER = {"id": "q-rem", "text": "Define resistivity.", "answer": "Resistance of a 1 m long wire of 1 m² area.",
              "bloom_level": "REMEMBER", "difficulty": "EASY"}
Q_EXTRA = {"id": "q-extra", "text": "State Ohm's law.", "answer": "V is proportional to I.", "bloom_level": "REMEMBER"}

REQUEST = PrepRequest(grade=10, subject="Science", chapter="Electricity", board="CBSE",
                      levels=["APPLY", "REMEMBER"], candidates=[Q_REMEMBER, Q_EXTRA, Q_APPLY])


def rewording(text, **kw):
    return _Rewording(reworded_text=text, spoken_text=kw.get("spoken", text), expected_points=["p1", "p2"],
                      misconceptions=["m1", "m2"], hints=["gentle", "stronger"])


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
    return [_Verdict(index=i, answer_to_reworded="a", ok=True, problem="") for i in idx]


@pytest.mark.asyncio
async def test_happy_path_one_question_per_level_in_order_with_rubric():
    llm = FakeLLM([GOOD_APPLY, GOOD_REMEMBER], [_Verdicts(verdicts=ok(0, 1))])
    result = await prep_questions(llm, REQUEST, generation_model="gen", verify_model="ver")
    qs = result.questions
    # levels order, not candidate order; the extra REMEMBER candidate is ignored
    assert [(q.order, q.bloom_level, q.dataset_question_id) for q in qs] == [(1, "APPLY", "q-apply"), (2, "REMEMBER", "q-rem")]
    assert qs[0].reworded_text == GOOD_APPLY.reworded_text and qs[0].original_text == Q_APPLY["text"]
    assert (qs[0].topic_id, qs[0].subtopic_id, qs[0].concepts) == ("t1", "s1", ["Ohm's law"])
    assert qs[0].hints == ["gentle", "stronger"] and len(qs[0].expected_points) == 2
    assert [c[:2] for c in llm.calls] == [("_Rewording", "gen"), ("_Rewording", "gen"), ("_Verdicts", "ver")]
    assert '"dataset_answer": "R = V/I = 44 Ω"' in llm.calls[2][2]


@pytest.mark.asyncio
async def test_verify_failure_regenerates_once_then_falls_back_to_original_wording_keeping_the_rubric():
    bad = _Verdict(index=0, answer_to_reworded="x", ok=False, problem="answer changed")
    llm = FakeLLM([GOOD_APPLY, GOOD_REMEMBER, GOOD_APPLY],
                  [_Verdicts(verdicts=[bad] + ok(1)), _Verdicts(verdicts=[bad])])
    result = await prep_questions(llm, REQUEST, generation_model="gen", verify_model="ver")
    first = result.questions[0]
    assert first.reworded_text == first.original_text == Q_APPLY["text"]
    assert first.spoken_text == Q_APPLY["text"]       # no symbols to say in words here
    assert first.hints == ["gentle", "stronger"]      # rubric from the last draft still filled
    assert result.questions[1].reworded_text == GOOD_REMEMBER.reworded_text
    assert '"feedback": "answer changed"' in llm.calls[3][2] and "Define resistivity" not in llm.calls[3][2]
    assert len(llm.calls) == 5


@pytest.mark.asyncio
async def test_a_regenerated_item_that_passes_is_used():
    bad = _Verdict(index=1, answer_to_reworded="x", ok=False, problem="too easy now")
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
    assert "missing: 220" in _problems(orig, rewording("A heater on a plug point takes 5 A. What is its resistance?"))
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
    assert _problems("Which process is better and why? (2)", rewording("Which method is better? Why?")) is None


def test_a_line_break_ends_a_sentence():
    orig = Q_APPLY["text"]
    two_lines = "A heater on a plug point runs on 220 V and takes 5 A of current\nWhat is its resistance in ohms?"
    assert _problems(orig, rewording(two_lines)) is None
