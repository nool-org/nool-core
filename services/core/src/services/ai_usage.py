"""Persists every AI request Core makes - one ai_usage_events row per
generation/colearner call (cost from the service's X-Nool-Cost response
header) and per finished live voice test (cost from colearner's finish
payload) - labelled with ai_context's feature and student/teacher/school.
Read by the Super Admin's /admin/ai-usage (routes/admin_ai_usage.py).

Fully best-effort: written in its own short session (never the caller's
transaction), and any failure is a warning, never an error for the user.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

import httpx

from shared.logging import get_request_id
from src.domain.models import AiUsageEvent
from src.repositories import get_session
from src.services import ai_context

logger = logging.getLogger(__name__)

USD_TO_INR_RATE = 86.5  # same indicative rate as generation/colearner, for a header without "inr"


def _uuid(value: str | None) -> uuid.UUID | None:
    try:
        return uuid.UUID(value) if value else None
    except ValueError:
        return None


def _error_text(response: httpx.Response | None, error: BaseException | None) -> str | None:
    """Short and secret-free: the status and the service's error code, or the exception's class."""
    if response is not None and response.status_code >= 400:
        try:
            code = response.json()["error"]["code"]
        except Exception:
            code = None
        return f"HTTP {response.status_code}" + (f" {code}" if isinstance(code, str) else "")
    if error is not None:
        return f"{type(error).__name__}: {str(error)[:200]}" if str(error) else type(error).__name__
    return None


def _cost_of(response: httpx.Response | None) -> dict:
    raw = response.headers.get("x-nool-cost") if response is not None else None
    try:
        cost = json.loads(raw) if raw else {}
    except ValueError:
        logger.warning("ai_usage_bad_cost_header")
        cost = {}
    return cost if isinstance(cost, dict) else {}


def realtime_cost(cost: Any) -> dict:
    """Colearner's live voice finish payload `cost` (cost_service.realtime_cost_report)
    in X-Nool-Cost's shape."""
    if not isinstance(cost, dict):
        return {}
    tokens = cost.get("tokens") if isinstance(cost.get("tokens"), dict) else {}
    models = cost.get("models") if isinstance(cost.get("models"), dict) else {}
    t = lambda *keys: int(sum(float(tokens.get(k) or 0) for k in keys))  # noqa: E731
    return {
        "models": [m for m in (models.get("live_call"), models.get("transcription")) if m],
        "calls": 1,
        "input_tokens": t("text_in", "text_in_cached", "audio_in", "audio_in_cached", "transcribe_in"),
        "cached_input_tokens": t("text_in_cached", "audio_in_cached"),
        "output_tokens": t("text_out", "audio_out", "transcribe_out"),
        "usd": cost.get("total_usd") or 0,
        "inr": cost.get("total_inr"),
    }


async def record(service: str, endpoint: str, default_feature: str, *, started: float | None = None,
                 response: httpx.Response | None = None, error: BaseException | None = None,
                 cost: dict | None = None) -> None:
    """One ai_usage_events row. `started` is time.perf_counter() before the call
    (None = no latency, e.g. a live voice test); `cost` overrides the response's
    X-Nool-Cost header. Status: the response's, else 0 for a network error, else 200."""
    try:
        cost = cost if cost is not None else _cost_of(response)
        models = [str(m)[:100] for m in cost.get("models") or [] if m][:10]
        usd = float(cost.get("usd") or 0)
        inr = float(cost["inr"]) if cost.get("inr") is not None else usd * USD_TO_INR_RATE
        labels = ai_context.current(default_feature)
        event = AiUsageEvent(
            service=service, endpoint=endpoint[:300], feature=labels["Feature"][:50],
            model=models[0] if models else None, models=models,
            calls=int(cost.get("calls") or 0),  # model calls made; 0 when it failed before any
            input_tokens=int(cost.get("input_tokens") or 0),
            cached_input_tokens=int(cost.get("cached_input_tokens") or 0),
            output_tokens=int(cost.get("output_tokens") or 0),
            usd=round(usd, 6), inr=round(inr, 4),
            latency_ms=None if started is None else round((time.perf_counter() - started) * 1000),
            status=response.status_code if response is not None else (0 if error is not None else 200),
            error=_error_text(response, error),
            school_id=_uuid(labels.get("School")), teacher_id=_uuid(labels.get("Teacher")),
            student_id=_uuid(labels.get("Student")), request_id=get_request_id(),
        )
        async with get_session() as session:
            session.add(event)
            await session.commit()
    except Exception as exc:  # never let cost bookkeeping break the user's request
        logger.warning("ai_usage_record_failed", extra={"endpoint": endpoint, "reason": type(exc).__name__})
