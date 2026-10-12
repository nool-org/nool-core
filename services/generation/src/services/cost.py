"""Measured cost of every OpenAI call this service makes: one
`generation_call_cost` log line per call (tokens, USD/INR and who it was
for), summed per feature and per student by scripts/cost_report.py.
Same shape as colearner's cost_service.py."""

import json
import logging
from contextvars import ContextVar
from typing import Any

logger = logging.getLogger(__name__)

USD_TO_INR_RATE = 86.5  # indicative, same as colearner
PRICES_CHECKED = "2026-10-05"

# USD per 1M tokens - developers.openai.com/api/docs/pricing. gpt-5-mini and
# gpt-4o-mini-transcribe text/per-minute rates are colearner's (checked
# 2026-10-05); gpt-5.1 is its published standard tier. Reasoning tokens are
# already inside output tokens (billed as output) - logged, not re-added.
# ponytail: audio_in is the transcribe model's launch audio-token rate (the
# page now shows only text rates + $0.003/min) - re-check on the next price review.
MODEL_PRICING = {
    "gpt-5.1": {"input": 1.25, "cached_input": 0.125, "output": 10.00},
    "gpt-5-mini": {"input": 0.25, "cached_input": 0.025, "output": 2.00},
    "gpt-4o-mini-transcribe": {"input": 1.25, "audio_input": 3.00, "output": 5.00, "per_minute": 0.003},
}

# Who the current request's calls are for - X-Nool-* headers from Core,
# bound per request by main.py's middleware.
caller: ContextVar[dict[str, str]] = ContextVar("nool_caller", default={})
_CALLER_HEADERS = ("feature", "student", "teacher", "school")

# The current request's summed spend (new_spend()), bound by main.py's
# middleware and returned to Core as the X-Nool-Cost header, which Core
# persists (services/core/src/services/ai_usage.py). A dict, not a value, so
# calls made in child tasks (asyncio.gather) add to the same total.
spent: ContextVar[dict | None] = ContextVar("nool_spent", default=None)


def new_spend() -> dict:
    return {"models": [], "calls": 0, "input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0, "usd": 0.0}


def cost_header(spend: dict) -> str:
    """X-Nool-Cost: compact JSON of the request's summed spend."""
    return json.dumps({**spend, "usd": round(spend["usd"], 7)}, separators=(",", ":"))


def caller_of(headers: Any, endpoint: str) -> dict[str, str]:
    """The caller context from a request's headers (Core sets them; absent = unknown)."""
    found = {k: (headers.get(f"x-nool-{k}") or "")[:64] for k in _CALLER_HEADERS}
    return {"endpoint": endpoint, **{k: v for k, v in found.items() if v}}


def usd(model: str, *, input_tokens: int = 0, cached_input_tokens: int = 0, audio_input_tokens: int = 0,
        output_tokens: int = 0, seconds: float = 0.0) -> float | None:
    """USD of one call; None for a model with no price here (logged, never guessed)."""
    p = MODEL_PRICING.get(model)
    if p is None:
        return None
    text_in = input_tokens - cached_input_tokens - audio_input_tokens
    total = (text_in * p["input"] + cached_input_tokens * p.get("cached_input", p["input"])
             + audio_input_tokens * p.get("audio_input", p["input"]) + output_tokens * p["output"]) / 1_000_000
    return round(total + seconds / 60 * p.get("per_minute", 0.0), 7)


def log_call(model: str, reasoning_tokens: int = 0, **usage: Any) -> None:
    """One `generation_call_cost` line: model, tokens, usd/inr + caller context."""
    cost = usd(model, **usage)
    if (spend := spent.get()) is not None:
        if model not in spend["models"]:
            spend["models"].append(model)
        spend["calls"] += 1
        for key in ("input_tokens", "cached_input_tokens", "output_tokens"):
            spend[key] += usage.get(key, 0)
        spend["usd"] += cost or 0.0
    logger.info("generation_call_cost", extra={"cost": {
        "model": model, **caller.get(), **usage, "reasoning_tokens": reasoning_tokens,
        "usd": cost, "inr": None if cost is None else round(cost * USD_TO_INR_RATE, 5),
        "prices_checked": PRICES_CHECKED,
    }})


def log_response_usage(model: str, usage: Any) -> None:
    """Responses API usage (input_tokens, cached, output, reasoning)."""
    if usage is None:
        return
    log_call(
        model,
        input_tokens=usage.input_tokens,
        cached_input_tokens=getattr(usage.input_tokens_details, "cached_tokens", 0) or 0,
        output_tokens=usage.output_tokens,
        reasoning_tokens=getattr(usage.output_tokens_details, "reasoning_tokens", 0) or 0,
    )


def log_transcription_usage(model: str, usage: Any) -> None:
    """Transcription usage: token-billed ({input_tokens, input_token_details.audio_tokens,
    output_tokens}) or duration-billed ({seconds})."""
    if usage is None:
        return
    if getattr(usage, "type", None) == "duration":
        log_call(model, seconds=usage.seconds or 0.0)
    else:
        log_call(
            model,
            input_tokens=usage.input_tokens,
            audio_input_tokens=getattr(getattr(usage, "input_token_details", None), "audio_tokens", 0) or 0,
            output_tokens=usage.output_tokens,
        )
