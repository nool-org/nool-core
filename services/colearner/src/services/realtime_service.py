"""Realtime pipeline: live, full-duplex voice over WebRTC (OpenAI Realtime).

Unlike chained/multimodal (one recorded clip per turn -> STT -> LLM -> TTS),
the app streams its mic straight to OpenAI over WebRTC and hears the reply
as it's generated - server-side VAD decides when the student has finished,
and the student can interrupt. The app never sees the API key or the
instructions: it hands its SDP offer to us (via Core), we create the call
with the session config ("unified interface"), and hand back the SDP answer.

We then attach a *sideband* WebSocket to the same call and record the
transcript ourselves - the Bloom report is built from what the server heard,
never from anything the client claims. The report uses the same mapper as
the chained pipeline (chained_service.generate_bloom_report).

ponytail: calls live in-process memory, same v1 limitation as session_store
- one colearner replica; move to shared state if this ever scales out.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime

import httpx
from websockets.asyncio.client import connect as ws_connect

from src.config.settings import get_settings
from src.domain.schemas import SessionConfig
from src.services.chained_service import build_chained_system_prompt, generate_bloom_report
from src.services.cost_service import add_realtime_usage, add_transcription_usage, realtime_cost_report
from src.services.session_store import SessionState, session_store

logger = logging.getLogger(__name__)

REALTIME_MODEL = "gpt-realtime-2.1-mini"  # ~3.5x cheaper per test than gpt-realtime-2.1
TRANSCRIBE_MODEL = "gpt-4o-mini-transcribe"
VOICE = "marin"
_OPENAI = "https://api.openai.com/v1/realtime"
# After the session's time limit the co-learner gets this long to wrap up,
# then we hang up regardless - an abandoned call must not keep billing.
_GRACE_SECONDS = 30
# Finished/abandoned calls are kept this long (so a retried finish still
# gets its result), then dropped.
_KEEP_SECONDS = 3 * 3600

END_SESSION_TOOL = {
    "type": "function",
    "name": "end_session",
    "description": (
        "Call this right after you have spoken your concluding summary, once every "
        "question has been explored (or when told the time is up)."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}

_REALTIME_ADDENDUM = """

LIVE VOICE SESSION RULES (this is a real-time spoken call):
- Speak first: open with a short, warm greeting that names the chapter and ends with "Are you ready to begin?".
- Keep every turn to 1-2 short spoken sentences, then stop and let the student talk.
- Every turn after the student speaks is ONE reply: acknowledge and ask the next question together. Never send a separate filler turn like "let me set the stage" or "let me think of a question" - just ask it.
- If the student interrupts you, stop and listen - respond to what they said.
- If what you heard is unclear, empty, or just noise, briefly ask them to repeat - never count it as an answer.
- If the student explicitly asks for a hint or a tip, give ONE short guiding nudge on the current question (never the answer, never whether they were right), and stay on that question.
- Once the student has answered the last question, call the end_session tool (you'll then be asked to say goodbye). Do not say "[SESSION_COMPLETED]" aloud.
- If you receive "[TIME_UP]", call end_session.
"""

CLOSING_INSTRUCTIONS = (
    "The session is over. Speak your final turn now, in 2-3 short sentences: a warm, honest peer summary of the "
    "student's strong points and one thing to review (never say whether answers were right or wrong), then end with "
    "exactly: \"That concludes all questions for our session. Great job discussing these concepts today!\""
)


@dataclass
class RealtimeCall:
    call_id: str
    owner_ref: str
    state: SessionState
    # item_id -> {"role", "text"}, in the order the conversation created them,
    # so a late-arriving user transcription still lands in the right place.
    items: dict[str, dict] = field(default_factory=dict)
    ended: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None
    report: dict | None = None
    # Spoken student turns - the first is "yes, I'm ready", then one per question.
    spoken_turns: int = 0
    # Final answer is in: the model's own next reply is cancelled and
    # end_session forced instead (see _record) - it can't be trusted to stop.
    forcing_end: bool = False
    forced_end_sent: bool = False
    # Billed usage as OpenAI reports it (response.done / transcription events).
    usage: dict[str, float] = field(default_factory=dict)
    ended_at: datetime | None = None


_calls: dict[str, RealtimeCall] = {}


class RealtimeError(Exception):
    pass


def _api_key() -> str:
    key = get_settings().openai_api_key
    if not key:
        raise RealtimeError("OPENAI_API_KEY is not configured on the colearner service.")
    return key


def build_session(config: SessionConfig) -> dict:
    return {
        "type": "realtime",
        "model": REALTIME_MODEL,
        "instructions": build_chained_system_prompt(config) + _REALTIME_ADDENDUM,
        "audio": {
            "input": {
                "transcription": {
                    "model": TRANSCRIBE_MODEL,
                    "language": "en",
                    "prompt": f"A school student answering {config.subject} questions about {config.chapter_name}.",
                },
                "noise_reduction": {"type": "near_field"},
                # semantic_vad judges "finished a thought", not just silence -
                # a student pausing mid-answer isn't cut off. "low" = patient.
                "turn_detection": {"type": "semantic_vad", "eagerness": "low"},
            },
            "output": {"voice": VOICE},
        },
        "tools": [END_SESSION_TOOL],
        "tool_choice": "auto",
    }


async def create_call(*, sdp_offer: str, config: SessionConfig, owner_ref: str) -> tuple[str, str]:
    """Returns (sdp_answer, call_id) and starts the sideband recorder."""
    key = _api_key()
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(
            f"{_OPENAI}/calls",
            headers={"Authorization": f"Bearer {key}"},
            files={
                "sdp": (None, sdp_offer, "application/sdp"),
                "session": (None, json.dumps(build_session(config)), "application/json"),
            },
        )
    if response.status_code >= 300:
        logger.warning("realtime_call_create_failed status=%s body=%s", response.status_code, response.text[:500])
        raise RealtimeError("Couldn't start the live voice call.")
    call_id = response.headers.get("Location", "").rstrip("/").split("/")[-1]
    if not call_id:
        raise RealtimeError("Live voice call started without a call id.")

    _prune()
    call = RealtimeCall(call_id=call_id, owner_ref=owner_ref, state=session_store.create_session(config))
    _calls[call_id] = call
    call.task = asyncio.create_task(_sideband(call, key))
    return response.text, call_id


def _prune() -> None:
    now = datetime.now()
    for call_id, call in list(_calls.items()):
        if call.ended.is_set() and (now - call.state.created_at).total_seconds() > _KEEP_SECONDS:
            session_store.delete_session(call.state.session_id)
            del _calls[call_id]


async def _sideband(call: RealtimeCall, key: str) -> None:
    try:
        async with ws_connect(
            f"wss://api.openai.com/v1/realtime?call_id={call.call_id}",
            additional_headers={"Authorization": f"Bearer {key}"},
        ) as ws:
            # The model speaks first (greeting) - nothing arrives from the
            # student until they hear it.
            await ws.send(json.dumps({"type": "response.create"}))
            try:
                await asyncio.wait_for(_record(call, ws), timeout=call.state.config.session_max_time_seconds)
            except asyncio.TimeoutError:
                # Time's up: let the co-learner wrap up in its own voice,
                # then hang up after a short grace period either way.
                logger.info("realtime_call_time_limit call_id=%s", call.call_id)
                await ws.send(json.dumps({
                    "type": "conversation.item.create",
                    "item": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "[TIME_UP]"}]},
                }))
                await ws.send(json.dumps({"type": "response.create"}))
                try:
                    await asyncio.wait_for(_record(call, ws), timeout=_GRACE_SECONDS)
                except asyncio.TimeoutError:
                    pass
    except Exception:  # never let the recorder crash the service
        logger.exception("realtime_sideband_failed call_id=%s", call.call_id)
    finally:
        call.ended_at = datetime.now()
        call.ended.set()
        await _hangup(call.call_id, key)


async def _record(call: RealtimeCall, ws) -> None:
    async for raw in ws:
        event = json.loads(raw)
        kind = event.get("type", "")
        if kind in ("conversation.item.added", "conversation.item.created"):
            item = event.get("item") or {}
            if item.get("type") == "message" and item.get("role") in ("user", "assistant"):
                is_new = item["id"] not in call.items
                call.items.setdefault(item["id"], {"role": item["role"], "text": ""})
                # only speech counts - a "Get a tip" request or [TIME_UP] is text
                spoken = any(c.get("type") == "input_audio" for c in item.get("content") or [])
                if is_new and item["role"] == "user" and spoken:
                    call.spoken_turns += 1
        elif kind == "response.created":
            final_answer_in = call.spoken_turns >= call.state.config.num_questions + 1
            if final_answer_in and not call.forcing_end and not call.state.is_completed:
                call.forcing_end = True
                await ws.send(json.dumps({"type": "response.cancel"}))
                await ws.send(json.dumps({"type": "output_audio_buffer.clear"}))
        elif kind == "conversation.item.input_audio_transcription.completed":
            entry = call.items.setdefault(event["item_id"], {"role": "user", "text": ""})
            entry["text"] = (event.get("transcript") or "").strip()
            add_transcription_usage(call.usage, event.get("usage"))
        elif kind == "response.output_audio_transcript.done":
            entry = call.items.setdefault(event["item_id"], {"role": "assistant", "text": ""})
            entry["text"] = (event.get("transcript") or "").strip()
        elif kind == "response.done":
            add_realtime_usage(call.usage, (event.get("response") or {}).get("usage"))
            outputs = (event.get("response") or {}).get("output") or []
            end = next((o for o in outputs if o.get("type") == "function_call" and o.get("name") == "end_session"), None)
            if end and not call.state.is_completed:
                call.state.is_completed = True
                # The model tends to call end_session straight after a bare
                # acknowledgement - so the closing summary is always its own,
                # explicit final turn (tools off, so it can't end twice).
                await ws.send(json.dumps({
                    "type": "conversation.item.create",
                    "item": {"type": "function_call_output", "call_id": end.get("call_id"), "output": "{\"ok\": true}"},
                }))
                await ws.send(json.dumps({
                    "type": "response.create",
                    "response": {"instructions": CLOSING_INSTRUCTIONS, "tool_choice": "none"},
                }))
            elif call.forcing_end and not call.forced_end_sent and not call.state.is_completed:
                # the model's reply to the final answer was cancelled above -
                # now make it end the session (which then triggers the goodbye)
                call.forced_end_sent = True
                await ws.send(json.dumps({
                    "type": "response.create",
                    "response": {
                        "instructions": "The student has answered the final question. Call end_session now, without speaking.",
                        "tool_choice": {"type": "function", "name": "end_session"},
                    },
                }))
        elif kind == "error":
            logger.warning("realtime_event_error call_id=%s error=%s", call.call_id, event.get("error"))


async def _hangup(call_id: str, key: str) -> None:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(f"{_OPENAI}/calls/{call_id}/hangup", headers={"Authorization": f"Bearer {key}"})
    except httpx.HTTPError:
        pass


def transcript_turns(call: RealtimeCall) -> list[dict]:
    return [{"role": i["role"], "content": i["text"]} for i in call.items.values() if i["text"]]


async def finish_call(call_id: str, owner_ref: str) -> dict:
    """Ends the call (if still live) and scores what the server heard.
    Idempotent: a repeat finish returns the same result."""
    call = _calls.get(call_id)
    if call is None or call.owner_ref != owner_ref:
        raise KeyError(call_id)
    if call.report is None:
        if call.task and not call.task.done():
            call.task.cancel()
        await call.ended.wait()
        turns = transcript_turns(call)
        call.state.turns = turns
        student_answers = sum(1 for t in turns if t["role"] == "user")
        full_transcript = "\n".join(
            f"[{'CO-LEARNER' if t['role'] == 'assistant' else 'STUDENT'}]: {t['content']}" for t in turns
        )
        report = await generate_bloom_report(call.state, full_transcript) if student_answers else None
        call.report = {
            "report": report,
            "turns": turns,
            # first student turn is the "ready?" reply, not a graded answer
            "answered": max(student_answers - 1, 0),
            "model_completed": call.state.is_completed,
            # report's own LLM call is already in total_cost_usd (generate_bloom_report records it)
            "cost": realtime_cost_report(
                call.usage, REALTIME_MODEL, TRANSCRIBE_MODEL,
                report_cost_usd=call.state.total_cost_usd,
                duration_seconds=((call.ended_at or datetime.now()) - call.state.created_at).total_seconds(),
            ),
        }
    return call.report
