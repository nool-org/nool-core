import asyncio
import json
from types import SimpleNamespace

from src.api.routes import regrade
from src.services.evaluation import grader

_BODY = {"question": "Why does light bend in water?", "expected_points": ["its speed changes"],
         "misconceptions": ["light slows because water is heavy"], "transcript": "speed changes", "subject": "Science"}


class _FakeClient:
    def __init__(self, payload):
        self.calls = []

        async def create(**kwargs):
            self.calls.append(kwargs)
            usage = SimpleNamespace(prompt_tokens=100, completion_tokens=20, prompt_tokens_details=None)
            msg = SimpleNamespace(content=json.dumps(payload), refusal=None)
            return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=usage)

        self.chat = SimpleNamespace(completions=SimpleNamespace(create=create))


def test_empty_transcript_scores_zero_without_a_model_call():
    client = _FakeClient({})
    result = asyncio.run(grader.grade_answer("Q?", [], [], "   ", client=client))
    assert result["score"] == 0 and not client.calls


def test_parsed_result_with_score_capped_to_range():
    client = _FakeClient({"score_percent": 140, "good": " You named the speed change. ", "add": "Add which way it bends."})
    result = asyncio.run(grader.grade_answer("Q?", ["p"], ["m"], "speed changes", "Science", client=client))
    assert result == {"score": 100, "good": "You named the speed change.", "add": "Add which way it bends."}
    assert client.calls[0]["model"] == grader.GRADER_MODEL
    assert "speed changes" in client.calls[0]["messages"][1]["content"]
    low = asyncio.run(grader.grade_answer("Q?", [], [], "x", client=_FakeClient({"score_percent": -5, "good": "", "add": "a"})))
    assert low["score"] == 0


def test_route_requires_auth(client):
    assert client.post("/api/v1/colearner/grade-answer", json=_BODY).status_code == 401


def test_route_returns_the_grade_and_503_on_failure(client, monkeypatch):
    monkeypatch.setattr("src.api.deps.verify_token", lambda token: object())
    headers = {"Authorization": "Bearer t"}

    async def fake(question, expected_points, misconceptions, transcript, subject):
        assert (question, transcript, subject) == (_BODY["question"], "speed changes", "Science")
        return {"score": 70, "good": "g", "add": "a"}

    monkeypatch.setattr(regrade, "grade_answer", fake)
    resp = client.post("/api/v1/colearner/grade-answer", json=_BODY, headers=headers)
    assert resp.status_code == 200 and resp.json() == {"score": 70, "good": "g", "add": "a"}

    async def broken(*args):
        raise RuntimeError("no key")

    monkeypatch.setattr(regrade, "grade_answer", broken)
    assert client.post("/api/v1/colearner/grade-answer", json=_BODY, headers=headers).status_code == 503
