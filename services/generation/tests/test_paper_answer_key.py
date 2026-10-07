"""Paper answer-key generation with a scripted fake LLM - no network."""

import pytest
from fastapi.testclient import TestClient

from src.services.paper_answer_key import (
    AnswerKeyRequest, MarkingPoint, _Key, _Verdict, _Verdicts, answer_key, problem,
)

NUMERICAL = {"id": "q1", "text": "A 220 V supply drives 5 A through a heater. Find its resistance.",
             "question_type": "SHORT_ANSWER", "marks": 3, "bloom_level": "APPLY"}
MCQ = {"id": "q2", "text": "SI unit of resistance?", "question_type": "MCQ", "marks": 1,
       "options": ["volt", "ohm", "ampere", "watt"], "reference_answer": "ohm"}
REQUEST = AnswerKeyRequest(grade=10, subject="Science", items=[NUMERICAL, MCQ])


def key(answer, *marks):
    return _Key(answer=answer, marking_points=[MarkingPoint(point=f"step {i}", marks=m) for i, m in enumerate(marks)])


GOOD_NUM = key("R = V/I = 220/5 = 44 ohm", 1, 1, 1)
GOOD_MCQ = key("ohm - resistance is measured in ohms", 1)


class FakeLLM:
    def __init__(self, keys, verdicts):
        self.script = {_Key: list(keys), _Verdicts: list(verdicts)}
        self.calls = []

    async def __call__(self, *, model, effort, system, user, output_type):
        self.calls.append((output_type.__name__, model, user))
        return self.script[output_type].pop(0)


def ok(*ids):
    return [_Verdict(id=i, ok=True, problem="") for i in ids]


def test_marks_must_add_up_and_mcq_answer_must_be_an_option():
    item = REQUEST.items[0]
    assert problem(item, GOOD_NUM) is None
    assert "add up to 2" in problem(item, key("44 ohm", 1, 1))
    assert problem(item, key("44 ohm", 1.5, 1.5)) is None  # half marks allowed
    assert "positive" in problem(item, key("44 ohm", 3, 0))
    assert "option" in problem(REQUEST.items[1], key("resistance", 1))


@pytest.mark.asyncio
async def test_happy_path_generates_and_verifies_with_the_bank_answer_as_reference():
    llm = FakeLLM([GOOD_NUM, GOOD_MCQ], [_Verdicts(verdicts=ok("q1", "q2"))])
    result = await answer_key(llm, REQUEST, generation_model="gen", verify_model="ver")
    assert [i.id for i in result.items] == ["q1", "q2"] and result.failed_ids == []
    assert sum(p.marks for p in result.items[0].marking_points) == 3
    assert [c[:2] for c in llm.calls] == [("_Key", "gen"), ("_Key", "gen"), ("_Verdicts", "ver")]
    assert '"reference_answer": "ohm"' in llm.calls[1][2]


@pytest.mark.asyncio
async def test_bad_sum_is_regenerated_with_feedback_then_left_out_when_it_fails_again():
    llm = FakeLLM([key("44 ohm", 1, 1), GOOD_MCQ, key("44 ohm", 2)],
                  [_Verdicts(verdicts=ok("q2"))])  # second round never reaches verify
    result = await answer_key(llm, REQUEST, generation_model="gen", verify_model="ver")
    assert [i.id for i in result.items] == ["q2"] and result.failed_ids == ["q1"]
    assert "must be exactly 3" in llm.calls[3][2]


@pytest.mark.asyncio
async def test_verifier_rejection_retries_once_and_a_passing_retry_is_used():
    llm = FakeLLM([key("R = 40 ohm", 1, 1, 1), GOOD_MCQ, GOOD_NUM],
                  [_Verdicts(verdicts=[_Verdict(id="q1", ok=False, problem="wrong value")] + ok("q2")),
                   _Verdicts(verdicts=ok("q1"))])
    result = await answer_key(llm, REQUEST, generation_model="gen", verify_model="ver")
    assert {i.id: i.answer for i in result.items}["q1"] == GOOD_NUM.answer and result.failed_ids == []
    assert '"feedback": "wrong value"' in llm.calls[3][2]


def test_api_answers_503_without_a_key_and_runs_with_one(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    from src.main import create_app

    app = create_app()
    with TestClient(app) as client:
        body = REQUEST.model_dump()
        assert client.post("/v1/paper/answer-key", json=body).status_code == 503
        assert client.post("/v1/paper/answer-key", json={**body, "items": []}).status_code == 422
        app.state.llm = FakeLLM([GOOD_NUM, GOOD_MCQ], [_Verdicts(verdicts=ok("q1", "q2"))])
        response = client.post("/v1/paper/answer-key", json=body)
        assert response.status_code == 200
        assert [i["id"] for i in response.json()["items"]] == ["q1", "q2"]
