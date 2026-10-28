import asyncio

from src.services import homework_insight_service


def test_falls_back_to_a_generic_placeholder_when_no_api_key_is_configured(monkeypatch):
    monkeypatch.setattr(homework_insight_service, "openai_client", None)

    result = asyncio.run(homework_insight_service.generate_homework_insight(
        topic="Magnetic Effects",
        grade="Grade 10",
        subject="Science",
        mastery_percent=52,
        weak_bloom_level="ANALYZE",
        textbook_context="Some syllabus excerpt.",
    ))

    assert result["what_needs_understanding"] == "You need more practice with Magnetic Effects."
    assert result["references"] == [{"title": "Class notes", "subtitle": "Magnetic Effects"}]
    assert result["key_idea_title"] == "Magnetic Effects"
    assert result["connection_prompt"] == "How does Magnetic Effects connect to what you already know?"


def test_falls_back_when_the_llm_call_itself_raises(monkeypatch, caplog):
    class _BoomClient:
        class chat:
            class completions:
                @staticmethod
                async def create(**_kwargs):
                    raise RuntimeError("upstream exploded")

    monkeypatch.setattr(homework_insight_service, "openai_client", _BoomClient())

    result = asyncio.run(homework_insight_service.generate_homework_insight(
        topic="Force and Laws of Motion",
        grade="Grade 10",
        subject="Science",
        mastery_percent=61,
        weak_bloom_level="APPLY",
        textbook_context=None,
    ))

    assert result["key_idea_title"] == "Force and Laws of Motion"
    assert "homework_insight_generation_failed" in caplog.text


def test_uses_a_generic_grounding_note_when_no_textbook_context_is_given(monkeypatch):
    captured = {}

    class _FakeMessage:
        content = (
            '{"what_needs_understanding": "x", "references": [{"title": "t", "subtitle": "s"}], '
            '"key_idea_title": "k", "key_idea_body": "b", "connection_prompt": "c"}'
        )

    class _FakeChoice:
        message = _FakeMessage()

    class _FakeResponse:
        choices = [_FakeChoice()]

    class _FakeClient:
        class chat:
            class completions:
                @staticmethod
                async def create(*, model, messages, response_format):
                    captured["prompt"] = messages[-1]["content"]
                    return _FakeResponse()

    monkeypatch.setattr(homework_insight_service, "openai_client", _FakeClient())

    result = asyncio.run(homework_insight_service.generate_homework_insight(
        topic="Force and Laws of Motion",
        grade="Grade 10",
        subject="Science",
        mastery_percent=61,
        weak_bloom_level="APPLY",
        textbook_context=None,
    ))

    assert result == {
        "what_needs_understanding": "x",
        "references": [{"title": "t", "subtitle": "s"}],
        "key_idea_title": "k",
        "key_idea_body": "b",
        "connection_prompt": "c",
    }
    assert "No specific syllabus excerpt was provided" in captured["prompt"]


def test_real_client_is_async_with_timeout_and_retries(monkeypatch):
    import importlib

    from openai import AsyncOpenAI

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    try:
        mod = importlib.reload(homework_insight_service)
        assert isinstance(mod.openai_client, AsyncOpenAI)
        assert mod.openai_client.max_retries == 2
        assert mod.openai_client.timeout == mod._TIMEOUT_SECONDS
    finally:
        monkeypatch.delenv("OPENAI_API_KEY")
        importlib.reload(homework_insight_service)


def test_strict_schema_refusal_falls_back_and_malformed_references_are_dropped(monkeypatch):
    """AI-013: json_object mode let non-dict references through; a refusal crashed into the fallback opaquely."""
    from types import SimpleNamespace

    def client(message):
        async def create(**kwargs):
            client.kwargs = kwargs
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    args = dict(topic="Light", grade="Class 10", subject="Science", mastery_percent=40, weak_bloom_level="APPLY",
                textbook_context="Refraction of light")
    monkeypatch.setattr(homework_insight_service, "openai_client", client(SimpleNamespace(refusal="no", content=None)))
    assert asyncio.run(homework_insight_service.generate_homework_insight(**args)) == homework_insight_service._fallback("Light")

    body = ('{"what_needs_understanding": "x", "references": ["Refraction", {"title": "Refraction", "subtitle": "s"}], '
            '"key_idea_title": "k", "key_idea_body": "b", "connection_prompt": "c"}')
    fake = client(SimpleNamespace(refusal=None, content=body))
    monkeypatch.setattr(homework_insight_service, "openai_client", fake)
    out = asyncio.run(homework_insight_service.generate_homework_insight(**args))
    assert out["references"] == [{"title": "Refraction", "subtitle": "s"}]
    assert client.kwargs["response_format"]["json_schema"]["strict"] is True
    assert client.kwargs["messages"][1]["content"].endswith("<syllabus>Refraction of light</syllabus>")
