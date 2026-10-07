"""Paper Copilot planner + transcribe with a scripted fake LLM - no network."""

import base64

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from src.services.paper_plan import (
    QUICK_ACTIONS, PlanRequest, _Changes, _NewSpec, _Plan, _PlanOp, _Written, plan_paper,
)
from src.services.transcribe import MAX_AUDIO_BYTES, TranscribeRequest, get_transcriber

BASE = {
    "board": "CBSE", "grade": 10, "subject": "Science", "total_marks": 20,
    "chapters": [{"id": "ch1", "name": "Light"}],
    "questions": [
        {"id": "q1", "number": 1, "text": "Define refraction.", "question_type": "SHORT_ANSWER", "marks": 2,
         "bloom_level": "REMEMBER", "difficulty": "EASY", "chapter_id": "ch1"},
        {"id": "q2", "number": 2, "text": "State Snell's law.", "question_type": "SHORT_ANSWER", "marks": 2,
         "bloom_level": "REMEMBER", "difficulty": "EASY", "chapter_id": "ch1"},
    ],
    "bank_candidates": [
        {"id": "b1", "text": "A ray enters glass at 30 deg; find the refracted angle (n=1.5).",
         "question_type": "SHORT_ANSWER", "marks": 3, "bloom_level": "APPLY", "difficulty": "MEDIUM",
         "chapter_id": "ch1", "topic_id": "t1", "answer": "19.5 deg"},
    ],
}


class FakeLLM:
    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.calls = []

    async def __call__(self, *, model, effort, system, user, output_type):
        self.calls.append((output_type.__name__, model, system, user))
        out = self.outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


def spec(**kw):
    return _NewSpec(**{"bank_candidate_id": None, "write_brief": None, "question_type": "SHORT_ANSWER", "marks": 3,
                       "bloom_level": "APPLY", "difficulty": "MEDIUM", "chapter_id": "ch1", "topic_id": None, **kw})


def op(kind, **kw):
    return _PlanOp(**{"op": kind, "question_id": None, "section_name": None, "position": None,
                      "ordered_question_ids": None, "changes": None, "new_question": None, "reason": "r", **kw})


def req(**kw):
    return PlanRequest(**{**BASE, **kw})


@pytest.mark.asyncio
async def test_bank_candidate_is_copied_without_a_write_call():
    llm = FakeLLM(_Plan(reply="ok", ops=[op("add", new_question=spec(bank_candidate_id="b1"))]))
    result = await plan_paper(llm, req(message="add an application question"), plan_model="mini", write_model="big")
    nq = result.ops[0].new_question
    assert (nq.source, nq.dataset_question_id, nq.text, nq.answer, nq.topic_id) == (
        "bank", "b1", BASE["bank_candidates"][0]["text"], "19.5 deg", "t1")
    assert [c[:2] for c in llm.calls] == [("_Plan", "mini")]


@pytest.mark.asyncio
async def test_without_a_bank_fit_the_writer_model_writes_the_exact_spec():
    written = _Written(text="Why does a pencil in water look bent?", options=None, answer="Refraction ...")
    llm = FakeLLM(_Plan(reply="ok", ops=[op("replace", question_id="q1", new_question=spec(write_brief="bent pencil"))]),
                  written)
    result = await plan_paper(llm, req(message="harder q1"), plan_model="mini", write_model="big")
    nq = result.ops[0].new_question
    assert (nq.source, nq.dataset_question_id, nq.marks, nq.bloom_level, nq.text) == ("ai", None, 3, "APPLY", written.text)
    name, model, system, user = llm.calls[1]
    assert (name, model) == ("_Written", "big")
    assert "Class 10" in system and "3 mark" in system and "APPLY" in system and "Light" in system
    assert "Define refraction." in user  # the replaced question is shown to the writer


@pytest.mark.asyncio
async def test_failed_or_unresolvable_ops_are_dropped_and_the_rest_kept():
    bad_mcq = _Written(text="Pick one", options=["a", "b"], answer="c")
    llm = FakeLLM(_Plan(reply="ok", ops=[
        op("add", new_question=spec(question_type="MCQ", write_brief="x")),   # writer returns a broken MCQ
        op("add", new_question=spec()),                                        # no bank id, no brief
        op("edit", question_id="q2", changes=_Changes(rewrite_brief=None, marks=None, bloom_level=None,
                                                      difficulty=None, question_type=None)),  # no change
        op("remove", question_id="q2"),
    ]), bad_mcq)
    result = await plan_paper(llm, req(message="x"), plan_model="mini", write_model="big")
    assert [o.op for o in result.ops] == ["remove"]


@pytest.mark.asyncio
async def test_rewrite_brief_edits_text_and_keeps_other_changes():
    llm = FakeLLM(_Plan(reply="ok", ops=[op("edit", question_id="q2", changes=_Changes(
        rewrite_brief="simpler words", marks=None, bloom_level=None, difficulty=None, question_type=None))]),
        _Written(text="What does Snell's law say?", options=["x"], answer="sin i / sin r is constant"))
    result = await plan_paper(llm, req(quick_action="simplify_wording"), plan_model="mini", write_model="big")
    ch = result.ops[0].changes
    assert (ch.text, ch.options, ch.marks) == ("What does Snell's law say?", None, None)


@pytest.mark.asyncio
async def test_quick_action_maps_to_its_fixed_instruction_and_planner_ops_are_capped():
    llm = FakeLLM(_Plan(reply="ok", ops=[op("remove", question_id="q1")] * 12))
    result = await plan_paper(llm, req(quick_action="fix_total_marks"), plan_model="mini", write_model="big")
    assert len(result.ops) == 10
    assert QUICK_ACTIONS["fix_total_marks"] in llm.calls[0][3]
    assert set(QUICK_ACTIONS) == {"harder", "easier", "more_application", "balance_chapters", "simplify_wording",
                                  "fix_total_marks"}


def test_request_needs_a_message_or_quick_action():
    with pytest.raises(ValidationError):
        req()
    with pytest.raises(ValidationError):
        req(quick_action="nonsense")


def test_transcribe_request_limits():
    ok = TranscribeRequest(audio_base64=base64.b64encode(b"abc").decode(), mime_type="audio/mpeg")
    assert ok.audio() == b"abc" and ok.filename() == "audio.mp3"
    with pytest.raises(ValueError):
        TranscribeRequest(audio_base64="!!notbase64").audio()
    with pytest.raises(ValidationError):
        TranscribeRequest(audio_base64="A" * (MAX_AUDIO_BYTES * 2))


def test_api_503_without_key_then_plan_and_transcribe(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    from src.main import create_app

    app = create_app()
    audio = {"audio_base64": base64.b64encode(b"voice").decode(), "prompt": "Light, refraction"}
    with TestClient(app) as client:
        body = {**BASE, "message": "remove q1"}
        assert client.post("/v1/paper/plan", json=body).status_code == 503
        assert client.post("/v1/transcribe", json=audio).status_code == 503

        app.state.llm = FakeLLM(_Plan(reply="Removed it.", ops=[op("remove", question_id="q1")]))
        response = client.post("/v1/paper/plan", json=body)
        assert response.status_code == 200
        assert response.json()["ops"][0] == {
            "op": "remove", "question_id": "q1", "section_name": None, "position": None,
            "ordered_question_ids": None, "changes": None, "new_question": None, "reason": "r"}

        seen = {}

        async def fake_transcriber(*, audio, filename, prompt):
            seen.update(audio=audio, filename=filename, prompt=prompt)
            return " make question 3 harder "

        app.dependency_overrides[get_transcriber] = lambda: fake_transcriber
        response = client.post("/v1/transcribe", json=audio)
        assert response.json() == {"text": "make question 3 harder"}
        assert seen == {"audio": b"voice", "filename": "audio.m4a", "prompt": "Light, refraction"}
        assert client.post("/v1/transcribe", json={"audio_base64": "!!"}).status_code == 422
