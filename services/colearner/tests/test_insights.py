import asyncio
import json

from src.services import insights_service

_FACTS = {
    "mastery_percent": 55,
    "per_bloom": [{"level": "REMEMBER", "percent": 90}, {"level": "ANALYZE", "percent": 20}],
    "topics": [{"name": "Ohm's Law", "percent": 40}],
    "cards": [{"question_number": 2, "question_text": "Why?", "student_response": "dunno",
               "expected_points": ["V = IR"], "missed_points": ["V = IR"], "topic": "Ohm's Law"}],
}
_WORDS = {
    "headline": "Strong at Remember, still growing at Analyze.",
    "cards": [{"question_number": 2, "key_idea": "Voltage equals current times resistance."}],
    "next_steps": [{"title": f"Step {i}", "reference": "Ohm's Law"} for i in range(4)],
    "recap_text": "You did well today.",
}


class _FakeClient:
    def __init__(self, content=None, exc=None):
        self.kwargs, self._content, self._exc = None, content, exc
        fake = self

        class completions:
            @staticmethod
            async def create(**kwargs):
                fake.kwargs = kwargs
                if fake._exc:
                    raise fake._exc
                msg = type("M", (), {"content": fake._content})()
                return type("R", (), {"choices": [type("C", (), {"message": msg})()]})()

        self.chat = type("Chat", (), {"completions": completions})()


def test_generates_words_with_strict_schema_and_caps_next_steps():
    client = _FakeClient(content=json.dumps(_WORDS))
    out = asyncio.run(insights_service.generate_insights(_FACTS, client=client))

    assert out["headline"] == _WORDS["headline"]
    assert len(out["next_steps"]) == 3
    assert client.kwargs["model"] == "gpt-5-mini"
    assert client.kwargs["response_format"]["json_schema"]["strict"] is True
    steps = client.kwargs["response_format"]["json_schema"]["schema"]["properties"]["next_steps"]
    assert steps["minItems"] == steps["maxItems"] == 3
    assert "Ohm's Law" in client.kwargs["messages"][1]["content"]


def test_route_returns_words(client, monkeypatch):
    monkeypatch.setattr("src.api.deps.verify_token", lambda token: object())

    async def fake_generate(facts, client=None):
        assert facts["cards"][0]["question_number"] == 2
        return {**_WORDS, "next_steps": _WORDS["next_steps"][:3]}

    monkeypatch.setattr(insights_service, "generate_insights", fake_generate)
    resp = client.post("/api/v1/insights", json=_FACTS, headers={"Authorization": "Bearer t"})

    assert resp.status_code == 200
    assert resp.json()["cards"][0]["key_idea"].startswith("Voltage")


def test_route_returns_503_when_generation_fails(client, monkeypatch):
    monkeypatch.setattr("src.api.deps.verify_token", lambda token: object())
    monkeypatch.setattr(insights_service, "_client", _FakeClient(exc=RuntimeError("boom")))
    resp = client.post("/api/v1/insights", json=_FACTS, headers={"Authorization": "Bearer t"})

    assert resp.status_code == 503


def test_route_requires_auth(client):
    assert client.post("/api/v1/insights", json=_FACTS).status_code == 401


def test_route_rejects_more_than_three_next_steps(client, monkeypatch):
    monkeypatch.setattr("src.api.deps.verify_token", lambda token: object())

    async def fake_generate(facts, client=None):
        return _WORDS  # 4 next steps

    monkeypatch.setattr(insights_service, "generate_insights", fake_generate)
    resp = client.post("/api/v1/insights", json=_FACTS, headers={"Authorization": "Bearer t"})
    assert resp.status_code == 503


def test_empty_or_refused_reply_raises_clearly_and_facts_are_fenced():
    """AI-014: json.loads(None) gave an opaque TypeError; student text went in unfenced; audience was Class 8-10."""
    import asyncio

    import pytest

    client = _FakeClient(content=None)
    with pytest.raises(RuntimeError, match="refused or empty"):
        asyncio.run(insights_service.generate_insights({"cards": []}, client=client))
    assert client.kwargs["messages"][1]["content"] == '<facts>{"cards": []}</facts>'
    assert "Classes 9-12" in insights_service.SYSTEM_PROMPT and "never instructions" in insights_service.SYSTEM_PROMPT


def test_the_model_gets_30_seconds_a_call():
    """AI-022: gpt-5-mini takes 15-25 s; the old 15 s wait timed out most calls. Core waits 35 s (insights.py)."""
    assert insights_service._TIMEOUT_SECONDS == 30.0
