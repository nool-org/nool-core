"""shared/llm_gateway.py: through LiteLLM when configured, with who/what each
call is for; straight to OpenAI (and nothing extra) when it isn't."""

import json

from shared import llm_gateway


def test_off_by_default_straight_to_openai(monkeypatch):
    monkeypatch.delenv("LLM_GATEWAY_URL", raising=False)
    assert llm_gateway.client_kwargs("sk-real") == {"api_key": "sk-real"}
    assert llm_gateway.call_kwargs("responses") == {}


def test_through_the_gateway_with_the_callers_metadata(monkeypatch):
    monkeypatch.setenv("LLM_GATEWAY_URL", "http://litellm:4000")
    monkeypatch.setenv("LLM_GATEWAY_KEY", "sk-gw")
    monkeypatch.setenv("SERVICE_NAME", "generation")
    assert llm_gateway.client_kwargs("sk-real") == {"api_key": "sk-gw", "base_url": "http://litellm:4000/v1"}
    token = llm_gateway.bind({"x-nool-feature": "homework", "x-nool-school": "s1", "x-nool-student": "st1"})
    try:
        responses = llm_gateway.call_kwargs("responses")
        chat = llm_gateway.call_kwargs("chat")
        audio = llm_gateway.call_kwargs("audio")
    finally:
        llm_gateway.reset(token)
    meta = {"service": "generation", "feature": "homework", "school": "s1", "student": "st1"}
    assert responses["extra_body"]["litellm_metadata"]["spend_logs_metadata"] == meta  # /responses' field
    assert chat["extra_body"]["metadata"]["spend_logs_metadata"] == meta
    assert "feature:homework" in responses["extra_headers"]["x-litellm-tags"]
    assert json.loads(chat["extra_headers"]["x-litellm-spend-logs-metadata"]) == meta
    assert responses["user"] == "st1" and "user" not in audio  # transcription takes no end user
