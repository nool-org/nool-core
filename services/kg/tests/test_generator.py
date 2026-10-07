"""generator.py + the two generate routes, against a fake AsyncOpenAI
client - no network, no API key."""

import json
from types import SimpleNamespace

import httpx
import pytest

from src.api.routes import question_paper, voice_test
from src.main import create_app
from src.services import generator

QUESTION = {
    "text": "What is a balanced equation?", "answer": "Equal atoms on each side.", "bloom_level": "REMEMBER",
    "difficulty": "EASY", "question_type": "short_answer", "chapter_number": 1, "topic": "1.1",
    "source_refs": ["CH01_T01"],
}
CONTEXT = [{"chapter_number": 1, "topic": "1.1", "topic_ref": "CH01_T01", "text_content": "..."}]


class FakeOpenAI:
    def __init__(self, payload):
        self.calls = []
        self.payload = payload
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.calls.append(kwargs)
        message = SimpleNamespace(content=json.dumps(self.payload), refusal=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


@pytest.fixture
def fake(monkeypatch):
    def install(payload):
        client = FakeOpenAI(payload)
        monkeypatch.setattr(generator, "_get_client", lambda api_key: client)
        return client
    return install


def test_shared_client_is_async_with_timeout_and_retries():
    client = generator._get_client("sk-test")
    assert client is generator._get_client("sk-test")
    assert client.timeout == 90.0
    assert client.max_retries == 2


@pytest.mark.asyncio
async def test_generate_questions_uses_the_request_board_grade_subject(fake):
    client = fake({"questions": [QUESTION]})
    questions = await generator.generate_questions(
        api_key="k", board="ICSE", grade=9, subject="Mathematics", concept_context=CONTEXT,
        bloom_distribution={"REMEMBER": 1}, difficulty_distribution={"EASY": 1}, question_types=[],
        total_questions=1,
    )
    assert questions == [QUESTION]
    system = client.calls[0]["messages"][0]["content"]
    assert system.startswith("You are a ICSE Class 9 Mathematics question-paper writer.")
    assert "CBSE" not in system


@pytest.mark.asyncio
async def test_generate_questions_skips_the_model_with_no_grounding(fake):
    client = fake({"questions": []})
    assert await generator.generate_questions(
        api_key="k", board="CBSE", grade=10, subject="Science", concept_context=[],
        bloom_distribution={}, difficulty_distribution={}, question_types=[], total_questions=5,
    ) == []
    assert client.calls == []


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    async def context(**kwargs):
        return CONTEXT
    monkeypatch.setattr(question_paper, "get_concept_context_for_scope", context)
    monkeypatch.setattr(voice_test, "get_concept_context_for_scope", context)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()), base_url="http://test")


@pytest.mark.asyncio
async def test_question_paper_generate_route(api, fake):
    client = fake({"questions": [QUESTION]})
    async with api:
        response = await api.post("/question-papers/generate", json={
            "subject": "Science", "board": "CBSE", "grade": 10, "chapters": [{"number": 1, "name": "Ch 1"}],
            "total_questions": 1,
        })
    assert response.status_code == 200, response.text
    assert response.json()["questions"] == [QUESTION]
    assert "CBSE Class 10 Science" in client.calls[0]["messages"][0]["content"]


@pytest.mark.asyncio
async def test_voice_test_generate_route_returns_context_and_no_questions(api, fake):
    client = fake({"textbook_context": "Rusting is..."})
    async with api:
        response = await api.post("/voice-tests/generate", json={
            "chapters": [{"number": 1, "name": "Ch 1"}], "bloom_levels": ["APPLY"], "num_questions": 2,
        })
    assert response.status_code == 200, response.text
    assert response.json() == {"reference_questions": [], "textbook_context": "Rusting is..."}
    call = client.calls[0]
    assert call["response_format"]["json_schema"]["schema"]["required"] == ["textbook_context"]
    assert "reference_questions" not in call["messages"][0]["content"]


@pytest.mark.asyncio
async def test_assessor_content_skips_the_model_with_no_grounding(fake):
    client = fake({"textbook_context": "x"})
    assert await generator.generate_assessor_content(api_key="k", concept_context=[]) == {
        "reference_questions": [], "textbook_context": "",
    }
    assert client.calls == []


@pytest.mark.asyncio
async def test_oversized_request_is_rejected_before_any_model_call(api, fake):
    client = fake({"questions": []})
    async with api:
        response = await api.post("/question-papers/generate", json={
            "chapters": [{"number": 1, "name": "Ch 1"}], "total_questions": 1, "topic_names": ["t"] * 301,
        })
    assert response.status_code == 422
    assert client.calls == []


def test_slots_replace_the_distributions_in_the_prompt():
    slots = [{"section": "A", "marks": 1, "bloom_level": "REMEMBER", "difficulty": "EASY", "question_type": "mcq"},
             {"section": "B", "marks": 5, "bloom_level": "APPLY", "difficulty": "HARD", "question_type": "long_answer"}]
    msg = generator._build_user_message(
        concept_context=CONTEXT, bloom_distribution={"REMEMBER": 25}, difficulty_distribution={},
        question_types=[], total_questions=2, slots=slots)
    assert "exactly 2 questions, IN THIS ORDER" in msg
    assert json.dumps(slots) in msg
    assert "level: count" not in msg
