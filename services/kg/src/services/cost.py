"""What kg's own model calls cost - summed per request and returned to Core
as X-Nool-Cost (the same header and shape as the generation service's
services/cost.py), so Core's ai_usage_events has every AI dollar, kg's too."""

import json
import logging
from contextvars import ContextVar
from typing import Any

logger = logging.getLogger(__name__)

# USD per 1M tokens - the generation service's table (services/generation/src/services/cost.py)
MODEL_PRICING = {
    "gpt-5.1": {"input": 1.25, "cached_input": 0.125, "output": 10.00},
    "gpt-5-mini": {"input": 0.25, "cached_input": 0.025, "output": 2.00},
}

spent: ContextVar[dict | None] = ContextVar("kg_spent", default=None)


def new_spend() -> dict:
    return {"models": [], "calls": 0, "input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0, "usd": 0.0}


def cost_header(spend: dict) -> str:
    return json.dumps({**spend, "usd": round(spend["usd"], 7)}, separators=(",", ":"))


def log_chat_usage(model: str, usage: Any) -> None:
    """A chat.completions response's usage, added to this request's spend."""
    if usage is None:
        return
    prompt = getattr(usage, "prompt_tokens", 0) or 0
    cached = getattr(getattr(usage, "prompt_tokens_details", None), "cached_tokens", 0) or 0
    output = getattr(usage, "completion_tokens", 0) or 0
    p = MODEL_PRICING.get(model)
    usd = ((prompt - cached) * p["input"] + cached * p["cached_input"] + output * p["output"]) / 1_000_000 if p else 0.0
    if (spend := spent.get()) is not None:
        if model not in spend["models"]:
            spend["models"].append(model)
        spend["calls"] += 1
        spend["input_tokens"] += prompt
        spend["cached_input_tokens"] += cached
        spend["output_tokens"] += output
        spend["usd"] += usd
    logger.info("kg_call_cost", extra={"cost": {"model": model, "input_tokens": prompt, "cached_input_tokens": cached,
                                                "output_tokens": output, "usd": round(usd, 7)}})
