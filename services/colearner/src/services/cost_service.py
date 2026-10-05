import json
from datetime import datetime
from typing import Any, Dict, Optional

import tiktoken

from src.config.settings import get_settings
from src.services.session_store import SessionState

# Pricing Catalog (per 1M tokens or per unit)
# Currency: USD
MODEL_PRICING = {
    # OpenAI Models
    "gpt-4o-mini": {
        "input_per_million": 0.15,
        "output_per_million": 0.60,
    },
    "gpt-5-mini": {
        "input_per_million": 0.25,
        "cached_input_per_million": 0.025,
        "output_per_million": 2.00,
    },
    "gpt-4o": {
        "input_per_million": 2.50,
        "output_per_million": 10.00,
    },
    "gpt-audio-mini": {
        "input_per_million": 0.15,
        "output_per_million": 0.60,
        "audio_input_per_million": 10.00,
        "audio_output_per_million": 20.00
    }
}

# STT & TTS Pricing
WHISPER_PER_SECOND_USD = 0.006 / 60.0  # $0.006 per minute = $0.0001 / sec
EDGE_TTS_PER_CHAR_USD = 0.0            # Edge-TTS is free / zero-cost
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


def get_tokenizer(model_name: str = "gpt-4o-mini"):
    """Returns tiktoken encoding for token estimation."""
    try:
        if "gpt-4o" in model_name:
            return tiktoken.get_encoding("o200k_base")
        return tiktoken.encoding_for_model(model_name)
    except Exception:
        try:
            return tiktoken.get_encoding("cl100k_base")
        except Exception:
            return None


def count_tokens(text: str, model_name: str = "gpt-4o-mini") -> int:
    """Counts tokens accurately using tiktoken, or falls back to ~4 characters per token."""
    if not text:
        return 0
    enc = get_tokenizer(model_name)
    if enc:
        try:
            return len(enc.encode(text))
        except Exception:
            pass
    # Fast fallback: 1 token ~ 4 English characters
    return max(1, len(text) // 4)


def estimate_audio_duration_seconds(audio_bytes: Optional[bytes]) -> float:
    """Estimates audio duration from audio bytes.
    For standard 16kHz 16-bit mono PCM WAV (32,000 bytes/sec),
    or approx 16-32 KB/sec for compressed webm/mp3.
    """
    if not audio_bytes:
        return 0.0
    size = len(audio_bytes)
    # Check if WAV header (first 4 bytes RIFF)
    if audio_bytes[:4] == b"RIFF" and size > 44:
        # 16000 samples/sec * 2 bytes/sample = 32000 bytes/sec
        return round(size / 32000.0, 2)
    # Fallback for compressed microphone audio (webm / mp3): approx 16 KB/sec
    return round(max(1.0, size / 16000.0), 2)


def format_pricing_rate(pricing: Dict[str, Any]) -> str:
    """Formats standard catalog pricing rate as human-readable string."""
    inp = pricing.get("input_per_million", 0.0)
    out = pricing.get("output_per_million", 0.0)
    inp_fmt = f"${inp:.3f}" if (inp > 0 and inp < 0.10) else f"${inp:.2f}"
    out_fmt = f"${out:.3f}" if (out > 0 and out < 0.10) else f"${out:.2f}"
    return f"{inp_fmt} / 1M in, {out_fmt} / 1M out"


def calculate_turn_cost(
    model_name: str,
    input_tokens: int,
    output_tokens: int,
    stt_seconds: float = 0.0,
    tts_chars: int = 0
) -> Dict[str, Any]:
    """Calculates exact USD cost for a single turn based on token consumption and audio processing."""
    pricing = MODEL_PRICING.get(model_name.lower())
    if not pricing:
        # Fallback to gpt-4o-mini rates if unknown
        pricing = MODEL_PRICING["gpt-4o-mini"]

    standard_rate = format_pricing_rate(pricing)
    input_cost = (input_tokens / 1_000_000.0) * pricing["input_per_million"]
    output_cost = (output_tokens / 1_000_000.0) * pricing["output_per_million"]
    stt_cost = stt_seconds * WHISPER_PER_SECOND_USD
    tts_cost = tts_chars * EDGE_TTS_PER_CHAR_USD

    total_cost = input_cost + output_cost + stt_cost + tts_cost

    return {
        "model_name": model_name,
        "standard_price_rate": standard_rate,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "stt_seconds": round(stt_seconds, 2),
        "tts_characters": tts_chars,
        "input_cost_usd": round(input_cost, 7),
        "output_cost_usd": round(output_cost, 7),
        "stt_cost_usd": round(stt_cost, 7),
        "tts_cost_usd": round(tts_cost, 7),
        "turn_cost_usd": round(total_cost, 6),
        "turn_cost_inr": round(total_cost * USD_TO_INR_RATE, 4)
    }


def record_turn_metrics(session: SessionState, turn_metrics: Dict[str, Any]) -> None:
    """Appends turn metrics and updates cumulative totals in SessionState."""
    session.turn_metrics.append(turn_metrics)
    session.total_input_tokens += turn_metrics.get("input_tokens", 0)
    session.total_output_tokens += turn_metrics.get("output_tokens", 0)
    session.total_audio_seconds += turn_metrics.get("stt_seconds", 0.0)
    session.total_cost_usd += turn_metrics.get("turn_cost_usd", 0.0)


def generate_cost_report(
    session: SessionState,
    pipeline_name: str,
    model_provider: str,
    model_name: str,
    tts_voice: str,
) -> Dict[str, Any]:
    """Generates a structured Cost Analysis Report and saves it to reports/.

    Left over from the removed per-turn chained pipeline (Whisper STT +
    Edge-TTS); nothing calls it today - realtime uses realtime_cost_report.
    model_provider/model_name/tts_voice are passed explicitly by the caller,
    since model selection is not part of SessionConfig.
    """
    elapsed_seconds = round((datetime.now() - session.created_at).total_seconds(), 1)
    total_tokens = session.total_input_tokens + session.total_output_tokens

    stt_total_cost = session.total_audio_seconds * WHISPER_PER_SECOND_USD
    llm_input_cost = sum(m.get("input_cost_usd", 0.0) for m in session.turn_metrics)
    llm_output_cost = sum(m.get("output_cost_usd", 0.0) for m in session.turn_metrics)
    total_cost_usd = round(stt_total_cost + llm_input_cost + llm_output_cost, 6)
    total_cost_inr = round(total_cost_usd * USD_TO_INR_RATE, 4)

    # Aggregate metrics by model
    models_breakdown: Dict[str, Any] = {}
    for m in session.turn_metrics:
        m_name = m.get("model_name", "unknown")
        if m_name not in models_breakdown:
            pricing = MODEL_PRICING.get(m_name.lower(), MODEL_PRICING["gpt-4o-mini"])
            models_breakdown[m_name] = {
                "model_name": m_name,
                "provider": model_provider,
                "role": "Conversation & Assessment LLM",
                "standard_price_rate": format_pricing_rate(pricing),
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "cost_usd": 0.0,
                "cost_inr": 0.0
            }
        models_breakdown[m_name]["input_tokens"] += m.get("input_tokens", 0)
        models_breakdown[m_name]["output_tokens"] += m.get("output_tokens", 0)
        models_breakdown[m_name]["total_tokens"] += m.get("total_tokens", 0)
        models_breakdown[m_name]["cost_usd"] += (m.get("input_cost_usd", 0.0) + m.get("output_cost_usd", 0.0))

    # Round model costs
    for m_name, m_data in models_breakdown.items():
        m_data["cost_usd"] = round(m_data["cost_usd"], 6)
        m_data["cost_inr"] = round(m_data["cost_usd"] * USD_TO_INR_RATE, 4)

    # Add Whisper STT if audio was ingested
    if session.total_audio_seconds > 0:
        stt_cost = round(session.total_audio_seconds * WHISPER_PER_SECOND_USD, 6)
        models_breakdown["whisper-1"] = {
            "model_name": "whisper-1",
            "provider": "openai",
            "role": "Speech-to-Text (STT)",
            "standard_price_rate": "$0.006 / min ($0.0001 / sec)",
            "audio_seconds_processed": round(session.total_audio_seconds, 2),
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cost_usd": stt_cost,
            "cost_inr": round(stt_cost * USD_TO_INR_RATE, 4)
        }

    # Add Edge-TTS
    total_tts_chars = sum(m.get("tts_characters", 0) for m in session.turn_metrics)
    models_breakdown[f"edge-tts ({tts_voice})"] = {
        "model_name": f"edge-tts ({tts_voice})",
        "provider": "microsoft",
        "role": "Text-to-Speech (TTS)",
        "standard_price_rate": "Free ($0.00)",
        "characters_synthesized": total_tts_chars,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cost_usd": 0.0,
        "cost_inr": 0.0
    }

    # Structured token usage per model
    token_usage_per_model = {
        m_name: {
            "input_tokens": m_data["input_tokens"],
            "output_tokens": m_data["output_tokens"],
            "total_tokens": m_data["total_tokens"]
        }
        for m_name, m_data in models_breakdown.items()
        if m_data.get("total_tokens", 0) > 0
    }

    report_data = {
        "report_type": "Cost & Resource Analysis",
        "session_id": session.session_id,
        "pipeline": pipeline_name,
        "model_provider": model_provider,
        "model_name": model_name,
        "session_max_time_limit_seconds": session.config.session_max_time_seconds,
        "session_duration_seconds": elapsed_seconds,
        "total_turns": session.turn_index,
        "token_usage": {
            "total_input_tokens": session.total_input_tokens,
            "total_output_tokens": session.total_output_tokens,
            "total_tokens": total_tokens,
            "approximate_words_processed": round(total_tokens * 0.75),
            "tokens_per_model": token_usage_per_model
        },
        "models_breakdown": models_breakdown,
        "audio_usage": {
            "total_audio_seconds_ingested": round(session.total_audio_seconds, 2),
            "stt_engine": "OpenAI Whisper (chained)" if "chained" in pipeline_name.lower() else "Direct Multimodal LLM",
            "tts_engine": f"Edge-TTS ({tts_voice})",
            "tts_cost_status": "Free (Edge-TTS zero-cost)"
        },
        "financial_summary": {
            "stt_cost_usd": round(stt_total_cost, 6),
            "llm_input_cost_usd": round(llm_input_cost, 6),
            "llm_output_cost_usd": round(llm_output_cost, 6),
            "tts_cost_usd": 0.0,
            "total_session_cost_usd": total_cost_usd,
            "total_session_cost_inr": total_cost_inr,
            "average_cost_per_turn_usd": round(total_cost_usd / max(1, session.turn_index), 6)
        },
        "turn_by_turn_breakdown": session.turn_metrics
    }

    # Save cost report to disk in reports/
    try:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        reports_dir = get_settings().reports_dir
        reports_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{pipeline_name.lower()}_cost_report_{session.session_id[:8]}_{timestamp}.json"
        filepath = reports_dir / filename
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(report_data, f, indent=2)
    except Exception as ex:
        print(f"Failed to write cost report to disk: {ex}")

    session.cost_report = report_data
    return report_data
