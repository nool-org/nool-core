"""HTTP client for the generation service's Paper Copilot endpoints - same
transport (and 503 error) as generation_client.py."""

from src.services.generation_client import _post

PLAN_TIMEOUT_SECONDS = 120.0
TRANSCRIBE_TIMEOUT_SECONDS = 60.0


async def plan(payload: dict) -> dict:
    """POST /v1/paper/plan - see services/generation/src/services/paper_plan.py: {reply, ops}."""
    return await _post("/v1/paper/plan", payload, PLAN_TIMEOUT_SECONDS, "paper")


async def transcribe(payload: dict) -> dict:
    """POST /v1/transcribe - {audio_base64, mime_type, prompt} -> {text}."""
    return await _post("/v1/transcribe", payload, TRANSCRIBE_TIMEOUT_SECONDS, "paper")
