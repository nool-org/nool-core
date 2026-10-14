"""The AI gateway: every OpenAI call the services make goes through the
LiteLLM proxy (compose service `litellm`) when LLM_GATEWAY_URL is set, and
carries who and what it was for - LiteLLM stores that with the call's cost
(its LiteLLM_SpendLogs table and UI), so spend can be traced by service,
feature, school, teacher, student and request.

Live voice (colearner's WebRTC realtime calls) can't go through a proxy -
audio flows between the phone and OpenAI - and keeps OPENAI_API_KEY; its
cost stays in Core's ai_usage_events.

Use:
    AsyncOpenAI(**llm_gateway.client_kwargs(settings.openai_api_key), timeout=...)
    await client.responses.parse(..., **llm_gateway.call_kwargs("responses"))
and bind the caller per request (each service's middleware):
    token = llm_gateway.bind(request.headers); ...; llm_gateway.reset(token)
"""

import json
import os
from contextvars import ContextVar, Token
from typing import Any, Literal

from shared.logging.context import get_request_id

# Core's X-Nool-* headers on every AI request (src/services/ai_context.py)
FIELDS = ("feature", "student", "teacher", "school")
_caller: ContextVar[dict[str, str]] = ContextVar("llm_gateway_caller", default={})


def enabled() -> bool:
    return bool(os.environ.get("LLM_GATEWAY_URL") and os.environ.get("LLM_GATEWAY_KEY"))


def bind(headers: Any) -> Token:
    """This request's caller, from Core's X-Nool-* headers."""
    found = {k: (headers.get(f"x-nool-{k}") or "")[:64] for k in FIELDS}
    return _caller.set({k: v for k, v in found.items() if v})


def reset(token: Token) -> None:
    _caller.reset(token)


def client_kwargs(api_key: str | None) -> dict[str, Any]:
    """AsyncOpenAI(**...) - the gateway when configured, else OpenAI directly."""
    if enabled():
        return {"api_key": os.environ["LLM_GATEWAY_KEY"],
                "base_url": os.environ["LLM_GATEWAY_URL"].rstrip("/") + "/v1"}
    return {"api_key": api_key}


def metadata() -> dict[str, str]:
    """Who/what this call is for: service, feature, school, teacher, student, request id."""
    meta = {"service": os.environ.get("SERVICE_NAME") or "", "request_id": get_request_id() or "",
            **_caller.get()}
    return {k: v for k, v in meta.items() if v}


def call_kwargs(api: Literal["responses", "chat", "audio"] = "chat") -> dict[str, Any]:
    """Per-call kwargs for the OpenAI SDK method: the metadata LiteLLM stores with
    the call (`litellm_metadata` on /responses, `metadata` elsewhere), tags to
    group spend by, and the end user. Nothing when the gateway is off - OpenAI
    itself doesn't take these."""
    if not enabled():
        return {}
    meta = metadata()
    # every field as a "key:value" tag too - LiteLLM keeps tags on every route, even for
    # a failed call, so a request is always traceable (spend by tag in its UI)
    tags = [f"{k}:{v}" for k, v in meta.items()]
    key = "litellm_metadata" if api == "responses" else "metadata"
    out: dict[str, Any] = {
        # spend_logs_metadata is what LiteLLM keeps with the call in its spend log
        "extra_body": {key: {"tags": tags, "spend_logs_metadata": meta}},
        # the same metadata as a header too - the one channel LiteLLM reads on every route
        "extra_headers": {"x-litellm-tags": ",".join(tags), "x-litellm-spend-logs-metadata": json.dumps(meta)},
    }
    user = meta.get("student") or meta.get("teacher")
    if user and api != "audio":
        out["user"] = user
    return out
