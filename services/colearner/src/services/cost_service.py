import json
from typing import Any, Dict, List, Optional

from src.services.session_store import SessionState

# Text-model pricing per 1M tokens, USD (used by the grader's turn cost).
MODEL_PRICING = {
    "gpt-4o-mini": {
        "input_per_million": 0.15,
        "output_per_million": 0.60,
    },
    "gpt-5-mini": {
        "input_per_million": 0.25,
        "cached_input_per_million": 0.025,
        "output_per_million": 2.00,
    },
}

USD_TO_INR_RATE = 86.5                 # Indicative currency conversion

# Realtime pipeline, per 1M tokens - developers.openai.com/api/docs/pricing,
# checked 2026-10-05. Audio and text are billed at different rates, and
# cached input (the conversation so far, re-read every turn) is far cheaper.
REALTIME_PRICING = {
    "gpt-realtime-2.1-mini": {"text_in": 0.60, "text_in_cached": 0.06, "text_out": 2.40,
                              "audio_in": 10.00, "audio_in_cached": 0.30, "audio_out": 20.00},
    "gpt-realtime-2.1": {"text_in": 4.00, "text_in_cached": 0.40, "text_out": 24.00,
                         "audio_in": 32.00, "audio_in_cached": 0.40, "audio_out": 64.00},
}
# ponytail: the page lists no audio-token rate for this model, only text
# rates + $0.003/min - token-billed transcription is priced at the text
# rates, so it may read low (it's a fraction of a cent per test either way).
TRANSCRIBE_PRICING = {"gpt-4o-mini-transcribe": {"text_in": 1.25, "text_out": 5.00, "per_minute": 0.003}}


def add_realtime_usage(totals: Dict[str, float], usage: Optional[Dict[str, Any]]) -> None:
    """Adds one `response.done` usage block (OpenAI Realtime) into totals."""
    if not usage:
        return
    inp = usage.get("input_token_details") or {}
    cached = inp.get("cached_tokens_details") or {}
    out = usage.get("output_token_details") or {}
    # text_tokens/audio_tokens include the cached part - split it out
    totals["text_in_cached"] = totals.get("text_in_cached", 0) + (cached.get("text_tokens") or 0)
    totals["audio_in_cached"] = totals.get("audio_in_cached", 0) + (cached.get("audio_tokens") or 0)
    totals["text_in"] = totals.get("text_in", 0) + (inp.get("text_tokens") or 0) - (cached.get("text_tokens") or 0)
    totals["audio_in"] = totals.get("audio_in", 0) + (inp.get("audio_tokens") or 0) - (cached.get("audio_tokens") or 0)
    totals["text_out"] = totals.get("text_out", 0) + (out.get("text_tokens") or 0)
    totals["audio_out"] = totals.get("audio_out", 0) + (out.get("audio_tokens") or 0)


def add_transcription_usage(totals: Dict[str, float], usage: Optional[Dict[str, Any]]) -> None:
    """Adds one input-transcription usage block (tokens- or duration-billed)."""
    if not usage:
        return
    if usage.get("type") == "duration":
        totals["transcribe_seconds"] = totals.get("transcribe_seconds", 0) + (usage.get("seconds") or 0)
    else:
        totals["transcribe_in"] = totals.get("transcribe_in", 0) + (usage.get("input_tokens") or 0)
        totals["transcribe_out"] = totals.get("transcribe_out", 0) + (usage.get("output_tokens") or 0)


def realtime_cost_report(
    totals: Dict[str, float], realtime_model: str, transcribe_model: str, report_cost_usd: float, duration_seconds: float
) -> Dict[str, Any]:
    """Cost of one realtime test: the live call + its transcription + the Bloom report call."""
    rp = REALTIME_PRICING[realtime_model]
    tp = TRANSCRIBE_PRICING[transcribe_model]
    per_m = lambda key, price: totals.get(key, 0) / 1_000_000 * price  # noqa: E731
    call_usd = sum(per_m(k, rp[k]) for k in rp)
    transcribe_usd = (
        per_m("transcribe_in", tp["text_in"]) + per_m("transcribe_out", tp["text_out"])
        + totals.get("transcribe_seconds", 0) / 60 * tp["per_minute"]
    )
    total = call_usd + transcribe_usd + report_cost_usd
    return {
        "total_usd": round(total, 6),
        "total_inr": round(total * USD_TO_INR_RATE, 4),
        "duration_seconds": round(duration_seconds, 1),
        "breakdown_usd": {
            "live_call": round(call_usd, 6),
            "transcription": round(transcribe_usd, 6),
            "bloom_report": round(report_cost_usd, 6),
        },
        "tokens": {k: int(v) if k != "transcribe_seconds" else round(v, 1) for k, v in totals.items()},
        "models": {"live_call": realtime_model, "transcription": transcribe_model},
        "prices_checked": "2026-10-05",
    }


def calculate_turn_cost(model_name: str, input_tokens: int, output_tokens: int) -> Dict[str, Any]:
    """USD cost of one text-model call from its token usage."""
    # Unknown models fall back to gpt-4o-mini rates
    pricing = MODEL_PRICING.get(model_name.lower(), MODEL_PRICING["gpt-4o-mini"])
    input_cost = (input_tokens / 1_000_000.0) * pricing["input_per_million"]
    output_cost = (output_tokens / 1_000_000.0) * pricing["output_per_million"]
    total_cost = input_cost + output_cost
    return {
        "model_name": model_name,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "input_cost_usd": round(input_cost, 7),
        "output_cost_usd": round(output_cost, 7),
        "turn_cost_usd": round(total_cost, 6),
        "turn_cost_inr": round(total_cost * USD_TO_INR_RATE, 4),
    }


def record_turn_metrics(session: SessionState, turn_metrics: Dict[str, Any]) -> None:
    """Appends turn metrics and adds the cost to the session total."""
    session.turn_metrics.append(turn_metrics)
    session.total_cost_usd += turn_metrics.get("turn_cost_usd", 0.0)


def cost_header(turn_metrics: List[Dict[str, Any]]) -> Optional[str]:
    """X-Nool-Cost for a request's text-model calls (the grader's turn metrics) -
    same compact JSON as generation's, persisted by Core (services/core/src/services/ai_usage.py).
    None when no call was made."""
    if not turn_metrics:
        return None
    models: List[str] = []
    for m in turn_metrics:
        if m.get("model_name") and m["model_name"] not in models:
            models.append(m["model_name"])
    total = sum(m.get("turn_cost_usd", 0.0) for m in turn_metrics)
    return json.dumps({
        "models": models, "calls": len(turn_metrics),
        **{k: sum(int(m.get(k) or 0) for m in turn_metrics)
           for k in ("input_tokens", "cached_input_tokens", "output_tokens")},
        "usd": round(total, 7), "inr": round(total * USD_TO_INR_RATE, 4),
    }, separators=(",", ":"))
