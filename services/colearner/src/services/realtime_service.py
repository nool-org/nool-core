"""Realtime pipeline - the production voice-test path: live, full-duplex
voice over WebRTC (OpenAI Realtime).

The app streams its mic straight to OpenAI over WebRTC and hears the reply
as it's generated - server-side VAD decides when the student has finished,
and the student can interrupt. The app never sees the API key or the
instructions: it hands its SDP offer to us (via Core), we create the call
with the session config ("unified interface"), and hand back the SDP answer.

We then attach a *sideband* WebSocket to the same call and record the
transcript ourselves - the Bloom report is built from what the server heard,
never from anything the client claims. The report comes from the strict
grader (src/services/evaluation).

With prepared questions (config.questions, contract C3) Milo asks exactly
those, in order, and the model calls ask_question / give_hint / end_session
tools - so the sideband knows which question
each student turn belongs to, and finish grades against each question's rubric.
Without them Milo improvises from the textbook context (the original flow).

ponytail: calls live in-process memory, same v1 limitation as session_store
- one colearner replica, lost on restart; move to shared state if this ever scales out.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime

import httpx
from websockets.asyncio.client import connect as ws_connect

from src.config.settings import get_settings
from src.domain.schemas import SessionConfig
from src.services.chained_service import build_chained_system_prompt
from src.services.evaluation import grade_transcript
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
# Cost caps - live voice is billed by how long Milo talks and how long the call runs.
# ~17 audio tokens per spoken second + the text transcript: 220 ~ 10 s of speech,
# a backstop for "1-2 short sentences" (a reply may be cut, never runs on).
MAX_REPLY_TOKENS = 220
MAX_CLOSING_TOKENS = 500  # the 2-3 sentence summary + fixed sign-off line
MAX_REPLIES = 18  # co-learner replies per call (floor), then the wrap-up is forced
REPLIES_PER_QUESTION = 6  # ask + one follow-up + two nudges + a silence check-in + acknowledgement


def reply_cap(num_questions: int) -> int:
    return max(MAX_REPLIES, REPLIES_PER_QUESTION * num_questions + 3)
MAX_CALL_SECONDS = 8 * 60  # config's session_max_time_seconds is unbounded
# Replies we start ourselves after a tool call: reading a prepared question
# (plus one context sentence) runs longer.
MAX_QUESTION_TOKENS = 400
MAX_HINTS = 2  # per question
# Student silent this long after Milo finished speaking a question -> one gentle check-in.
SILENCE_SECONDS = 12
EASIER = "easier"
STANDARD = "standard"

END_SESSION_TOOL = {
    "type": "function",
    "name": "end_session",
    "description": (
        "Call this right after you have spoken your concluding summary, once every "
        "question has been explored (or when told the time is up)."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}

_QUESTION_NUMBER = {
    "type": "object",
    "properties": {"question_number": {"type": "integer", "description": "1-based number of the question."}},
    "required": ["question_number"],
}
ASK_QUESTION_TOOL = {
    "type": "function",
    "name": "ask_question",
    "description": (
        "Call this, without speaking, right before you ask a question. It returns the exact text to say. "
        "Set easier=true to get a simpler version, when the student struggled on the previous question."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            **_QUESTION_NUMBER["properties"],
            "easier": {"type": "boolean", "description": "Ask the easier version of this question. Default false."},
        },
        "required": ["question_number"],
    },
}
GIVE_HINT_TOOL = {
    "type": "function",
    "name": "give_hint",
    "description": (
        "Call this, without speaking, when the student asks for a hint, is stuck or says they don't know. "
        "It returns a guiding nudge to say: the first points to the concept, the second is a smaller sub-question."
    ),
    "parameters": _QUESTION_NUMBER,
}

_REALTIME_ADDENDUM = """

LIVE VOICE SESSION RULES (this is a real-time spoken call):
- Speak first: open with a short, warm greeting that names the chapter and ends with "Are you ready to begin?".
- Keep every turn to 1-2 short spoken sentences (under 25 words in total), then stop and let the student talk.
- Never explain, recap or lecture, and never repeat what the student said back to them - one quick acknowledgement at most.
- "Are you ready to begin?" is asked ONCE, at the start. Never ask whether they are ready for the next question and never ask for any other confirmation between questions - after a short neutral acknowledgement, go straight on.
- Never say question numbers aloud (no "question two", "next is question 3"); "last one" is fine.
- If the student asks for the answer or asks you to explain, kindly say you can't give answers during the test but they should give it their best guess, and stay on the question.
- Every turn after the student speaks is ONE reply: acknowledge and ask the next question together. Never send a separate filler turn like "let me set the stage" or "let me think of a question" - just ask it.
- If the student interrupts you, stop and listen - respond to what they said.
- If what you heard is unclear, empty, or just noise, briefly ask them to repeat - never count it as an answer.
- If the student explicitly asks for a hint or a tip, give ONE short guiding nudge on the current question (never the answer, never whether they were right), and stay on that question.
- Once the student has answered the last question, call the end_session tool (you'll then be asked to say goodbye). Do not say "[SESSION_COMPLETED]" aloud.
- If you receive "[TIME_UP]", call end_session.
- If the student asks to stop, quit, exit or end the test early, kindly say the test can't be stopped midway and you'll finish it together, then repeat the current question. Never call end_session for that.
"""

CLOSING_INSTRUCTIONS = (
    "The session is over. Speak your final turn now, in 2-3 short sentences: thank the student by their first name "
    "if you know it, name one specific strength of their thinking or effort you noticed today, and one thing to "
    "review (never say whether answers were right or wrong), then end with "
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
    # [TIME_UP] was sent - ending now is allowed even with questions left
    time_up: bool = False
    # Spoken co-learner replies so far (response.done with a message), capped at MAX_REPLIES.
    replies: int = 0
    # Reply cap hit: hang up once the closing goodbye has finished playing.
    hangup_after_closing: bool = False
    closing_sent: bool = False
    closing_done: bool = False
    # Billed usage as OpenAI reports it (response.done / transcription events).
    usage: dict[str, float] = field(default_factory=dict)
    ended_at: datetime | None = None
    # Prepared-questions mode only: the question being asked (0 = none yet),
    # item_id -> question number / student-turn kind, and per-question progress
    # {first_item, hints_used, explanation_requested, version, repeat_requests, silences}
    # keyed by question number.
    current: int = 0
    item_question: dict[str, int] = field(default_factory=dict)
    item_kind: dict[str, str] = field(default_factory=dict)
    last_user_item: str | None = None
    progress: dict[int, dict] = field(default_factory=dict)
    # Pending [SILENCE] check-in (see _arm_silence), cancelled as soon as anyone speaks.
    silence_task: asyncio.Task | None = None
    student_speaking: bool = False


_calls: dict[str, RealtimeCall] = {}


class RealtimeError(Exception):
    pass


def _api_key() -> str:
    key = get_settings().openai_api_key
    if not key:
        raise RealtimeError("OPENAI_API_KEY is not configured on the colearner service.")
    return key


def build_question_instructions(config: SessionConfig) -> str:
    # Never the expected points: whatever is in here can end up said aloud.
    # The grader gets them separately (question_rubric).
    questions = "\n".join(f"Q{n} [{q.bloom_level}]: \"{q.spoken_text}\"" for n, q in enumerate(config.questions, 1))
    student = ""
    if config.student_name:
        student = (f"\nTHE STUDENT: their first name is \"{config.student_name}\". Use it in the greeting, "
                   "then at most once or twice more in the whole test.")
    if config.student_context:
        student += (f"\nTHEIR LAST TEST: \"{config.student_context}\" - add ONE short, encouraging sentence from it "
                    "to the greeting (like \"Last time you were great at remembering facts - today let's think a bit "
                    "more.\"). Never mention scores or numbers.")
    return f"""You are Milo, a warm teacher giving a short spoken {config.subject} test on "{config.chapter_name}" to a {config.grade} student in India. You guide the student's thinking - you never give the answers.
Speak simple Indian English, like a kind teacher in a South Indian school: short, clear sentences and everyday words.{student}

THE QUESTIONS - ask exactly these {len(config.questions)}, in this order. Never invent, skip, reorder, merge or change the meaning of a question:
{questions}

HOW THE SESSION GOES (this is a real-time spoken call):
- Speak first: a short, warm greeting that names the chapter, then ask "Are you ready?" and wait for the student. This is the ONLY confirmation in the whole test.
- Before each question, call ask_question with its number, without speaking. Then say the text it returns. You may add ONE short friendly sentence of context before it, but keep the question's meaning.
- Pick the difficulty per question: if the student struggled on the previous question (needed nudges, said they don't know, gave a very short or nervous answer, or was silent a long time), call ask_question with easier=true; if they answered confidently, ask the standard one (easier=false). Ask the first question standard unless the student sounded nervous during the greeting.
- Never say question numbers aloud (no "question two", "next is question 3"); "last one" is fine.
- Then stop and listen. Never say or hint whether an answer is right or wrong, never correct it, never praise it as correct, and never repeat it back.
- If the answer is vague or too short, ask ONE short follow-up (like "Can you tell me why?" or "What happens next?"), then move on whatever they say.
- At most ONCE in the whole test, right after a strong, confident answer, you may ask "How would you explain that to a friend?" - that is the one follow-up for that question; then move on.
- If the first answers are very short or hesitant, be extra encouraging with short, specific words like "Nice start, keep going" - still never say right or wrong.
- The student may mix English with Tamil, Hindi, Telugu, Kannada, Malayalam or another language (Tanglish, Hinglish). That's fine: understand the meaning, reply in simple English, and never correct their language or grammar.
- Otherwise give one short neutral acknowledgement (like "Okay, got it.") and go straight to the next question. Never ask whether they are ready for the next question and never ask for any other confirmation between questions.
- If the student is stuck, says "I don't know" or asks for a hint: call give_hint with the question number, without speaking, then say the guiding nudge it returns - a guiding question, never the answer. The first nudge points to the concept, the second is a smaller sub-question. At most 2 nudges per question; if it says no nudges are left, say something like "That's okay, we'll work on this in your homework" and go to the next question.
- If the student asks for the answer or asks you to explain: kindly say "I can't give answers during the test, but give it your best guess," and stay on the question. Never explain or teach the answer during the test - that comes later in their homework.
- Keep every other turn to 1-2 short sentences (under 25 words).
- If the student interrupts you, stop and listen - respond to what they said.
- If what you heard is unclear, empty, or just noise, briefly ask them to repeat - never count it as an answer.
- Once the student has answered the last question, call the end_session tool (you'll then be asked to say goodbye). Do not say "[SESSION_COMPLETED]" aloud.
- If you receive "[SILENCE]", the student has been quiet for a while: warmly say "Take your time. Want a small hint?" (one sentence) and wait.
- If you receive "[TIME_UP]", call end_session.
- If the student asks to stop, quit, exit or end the test early, kindly say the test can't be stopped midway and you'll finish it together, then repeat the current question. Never call end_session for that.
"""


def build_session(config: SessionConfig) -> dict:
    if config.questions:
        instructions = build_question_instructions(config)
        tools = [ASK_QUESTION_TOOL, GIVE_HINT_TOOL, END_SESSION_TOOL]
    else:
        instructions = build_chained_system_prompt(config) + _REALTIME_ADDENDUM
        tools = [END_SESSION_TOOL]
    return {
        "type": "realtime",
        "model": REALTIME_MODEL,
        "instructions": instructions,
        "audio": {
            "input": {
                "transcription": {
                    "model": TRANSCRIBE_MODEL,
                    "language": "en",
                    "prompt": f"A school student in India answering {config.subject} questions about "
                              f"{config.chapter_name}, sometimes mixing in Tamil, Hindi or other Indian-language words.",
                },
                # far_field: students use phones on speakerphone, so they can still barge in.
                "noise_reduction": {"type": "far_field"},
                # semantic_vad judges "finished a thought", not just silence -
                # a student pausing mid-answer isn't cut off. "medium" still lets them interrupt Milo.
                "turn_detection": {"type": "semantic_vad", "eagerness": "medium"},
            },
            "output": {"voice": VOICE},
        },
        "tools": tools,
        "tool_choice": "auto",
        "max_output_tokens": MAX_REPLY_TOKENS,
    }


def call_time_limit(config: SessionConfig) -> int:
    return min(config.session_max_time_seconds, MAX_CALL_SECONDS)


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
                await asyncio.wait_for(_record(call, ws), timeout=call_time_limit(call.state.config))
                if call.closing_done:  # reply cap hit: hang up once the goodbye has played
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(_record(call, ws), timeout=_GRACE_SECONDS)
            except asyncio.TimeoutError:
                # Time's up: let the co-learner wrap up in its own voice,
                # then hang up after a short grace period either way.
                logger.info("realtime_call_time_limit call_id=%s", call.call_id)
                call.time_up = True
                _cancel_silence(call)
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
        _cancel_silence(call)
        call.ended_at = datetime.now()
        call.ended.set()
        await _hangup(call.call_id, key)
        cost = _cost(call)
        logger.info(
            "realtime_call_cost call_id=%s duration_s=%s replies=%d total_usd=%s",
            call.call_id, cost["duration_seconds"], call.replies, cost["total_usd"],
            extra={"call_id": call.call_id, "duration_seconds": cost["duration_seconds"],
                   "replies": call.replies, "total_usd": cost["total_usd"]},
        )


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
                if is_new and call.current:
                    call.item_question[item["id"]] = call.current
                    if item["role"] == "user" and spoken:
                        _tag_user_turn(call, item["id"])
        elif kind in ("input_audio_buffer.speech_started", "input_audio_buffer.speech_stopped"):
            call.student_speaking = kind.endswith("started")
            _cancel_silence(call)
        elif kind == "output_audio_buffer.started":
            _cancel_silence(call)  # Milo is speaking
        elif kind == "response.created":
            _cancel_silence(call)
            # with prepared questions the final answer is detected at response.done (_questions_done)
            final_answer_in = (not call.state.config.questions
                               and call.spoken_turns >= call.state.config.num_questions + 1)
            # reply cap: the student answers Milo's last reply, then the wrap-up is forced the same way
            capped = call.replies >= reply_cap(call.state.config.num_questions)
            if (final_answer_in or capped) and not call.forcing_end and not call.state.is_completed:
                call.forcing_end = True
                if capped and not final_answer_in:
                    logger.info("realtime_call_reply_cap call_id=%s replies=%d", call.call_id, call.replies)
                    call.hangup_after_closing = True
                await ws.send(json.dumps({"type": "response.cancel"}))
                await ws.send(json.dumps({"type": "output_audio_buffer.clear"}))
        elif kind == "conversation.item.input_audio_transcription.completed":
            entry = call.items.setdefault(event["item_id"], {"role": "user", "text": ""})
            entry["text"] = (event.get("transcript") or "").strip()
            if not entry["text"] and call.item_kind.get(event["item_id"]) == "answer":
                _not_an_answer(call, event["item_id"])
            elif call.item_kind.get(event["item_id"]) in ("answer", "follow_up") and _repeat_request(entry["text"]):
                _not_an_answer(call, event["item_id"], "repeat_request")
            add_transcription_usage(call.usage, event.get("usage"))
        elif kind == "response.output_audio_transcript.done":
            entry = call.items.setdefault(event["item_id"], {"role": "assistant", "text": ""})
            entry["text"] = (event.get("transcript") or "").strip()
        elif kind == "response.done":
            add_realtime_usage(call.usage, (event.get("response") or {}).get("usage"))
            outputs = (event.get("response") or {}).get("output") or []
            if any(o.get("type") == "message" for o in outputs):
                call.replies += 1
                if call.closing_sent and call.hangup_after_closing:
                    call.closing_done = True
                    return  # goodbye generated - _sideband hangs up once it has played
            end = next((o for o in outputs if o.get("type") == "function_call" and o.get("name") == "end_session"), None)
            if call.state.config.questions and not end and not call.state.is_completed:
                await _questions_done(call, ws, outputs)
            if end and not call.state.is_completed and not _may_end(call):
                # the student asked to stop early - the test can't be ended midway
                logger.info("realtime_end_refused call_id=%s current=%s", call.call_id, call.current)
                await ws.send(json.dumps({
                    "type": "conversation.item.create",
                    "item": {"type": "function_call_output", "call_id": end.get("call_id"),
                             "output": json.dumps({"error": "The test can't end before every question is answered."})},
                }))
                await ws.send(json.dumps({
                    "type": "response.create",
                    "response": {"instructions": END_REFUSED_INSTRUCTIONS, "tool_choice": "none",
                                 "max_output_tokens": MAX_REPLY_TOKENS},
                }))
            elif end and not call.state.is_completed:
                call.state.is_completed = True
                # The model tends to call end_session straight after a bare
                # acknowledgement - so the closing summary is always its own,
                # explicit final turn (tools off, so it can't end twice).
                await ws.send(json.dumps({
                    "type": "conversation.item.create",
                    "item": {"type": "function_call_output", "call_id": end.get("call_id"), "output": "{\"ok\": true}"},
                }))
                call.closing_sent = True
                await ws.send(json.dumps({
                    "type": "response.create",
                    "response": {"instructions": CLOSING_INSTRUCTIONS, "tool_choice": "none",
                                 "max_output_tokens": MAX_CLOSING_TOKENS},
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
        elif kind == "output_audio_buffer.stopped":
            if call.closing_done:
                return  # goodbye has finished playing
            if call.state.config.questions:
                _arm_silence(call, ws)
        elif kind == "error":
            logger.warning("realtime_event_error call_id=%s error=%s", call.call_id, event.get("error"))


END_REFUSED_INSTRUCTIONS = (
    "The student wants to stop, but the test can't be ended midway. In one or two short, kind sentences say that "
    "you'll finish it together and it won't take long, then repeat the current question."
)


def _may_end(call: RealtimeCall) -> bool:
    """end_session is honoured only when the test is really over: the final
    answer is in (or the reply cap hit), or time is up - never because the
    student asked to quit early."""
    if call.forcing_end or call.time_up:
        return True
    questions = call.state.config.questions
    if questions:
        return call.current >= len(questions) and call.spoken_turns > len(questions)
    return call.spoken_turns >= call.state.config.num_questions + 1


def _progress(call: RealtimeCall, n: int) -> dict:
    return call.progress.setdefault(n, {"first_item": None, "hints_used": 0, "explanation_requested": False,
                                        "version": STANDARD, "repeat_requests": 0, "silences": 0})


def _awaiting_answer(call: RealtimeCall, n: int) -> bool:
    """Question n was asked, nobody is talking and its answer is still to come."""
    return (n == call.current and n > 0 and not call.student_speaking and not call.state.is_completed
            and not call.time_up and not call.forcing_end and _progress(call, n)["first_item"] is None)


def _cancel_silence(call: RealtimeCall) -> None:
    if call.silence_task:
        call.silence_task.cancel()
        call.silence_task = None


def _arm_silence(call: RealtimeCall, ws) -> None:
    """Milo's audio just finished: if the student then stays quiet for SILENCE_SECONDS,
    nudge Milo with [SILENCE] - once per question (mirrors the [TIME_UP] injection)."""
    _cancel_silence(call)
    n = call.current
    if _awaiting_answer(call, n) and not _progress(call, n)["silences"]:
        call.silence_task = asyncio.create_task(_silence_after(call, ws, n))


async def _silence_after(call: RealtimeCall, ws, n: int) -> None:
    await asyncio.sleep(SILENCE_SECONDS)
    call.silence_task = None
    if not _awaiting_answer(call, n) or _progress(call, n)["silences"]:
        return
    _progress(call, n)["silences"] += 1
    with contextlib.suppress(Exception):  # the call may have just closed
        await ws.send(json.dumps({
            "type": "conversation.item.create",
            "item": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "[SILENCE]"}]},
        }))
        await ws.send(json.dumps({"type": "response.create", "response": {"max_output_tokens": MAX_REPLY_TOKENS}}))


def _tag_user_turn(call: RealtimeCall, item_id: str) -> None:
    """The first spoken turn on a question is its answer; later ones are follow-ups
    (re-tagged as a hint/explanation request when the model's tool call says so)."""
    call.last_user_item = item_id
    progress = _progress(call, call.current)
    if progress["first_item"] is None:
        progress["first_item"] = item_id
        call.item_kind[item_id] = "answer"
    else:
        call.item_kind[item_id] = "follow_up"


def _request_turn(call: RealtimeCall, n: int, kind: str) -> None:
    """The student's latest turn was a request about question n, not an answer -
    even if Milo had already moved on and it was tagged with a later question."""
    item_id = call.last_user_item
    tagged = call.item_question.get(item_id)
    if item_id is None or tagged not in (n, call.current):  # an older question's turn - not this request
        return
    if _progress(call, tagged)["first_item"] == item_id:
        _progress(call, tagged)["first_item"] = None  # that question's answer is still to come
    call.item_question[item_id] = n
    call.item_kind[item_id] = kind


# Words a "say that again?" turn is made of - a turn using only these (and at
# least one of _REPEAT_KEYS) asks for a repeat, it doesn't answer.
# ponytail: word-list heuristic, transcript-based; add barge-in timing (output_audio_buffer events) if it misses.
_REPEAT_WORDS = set(
    "sorry pardon what huh wait repeat say it that again please can could you the question come one more time "
    "once i didn't did not hear get catch understand was milo sir ma'am miss okay ok hello".split()
)
_REPEAT_KEYS = {"pardon", "what", "huh", "wait", "repeat", "again", "hear", "get", "catch"}


def _repeat_request(text: str) -> bool:
    words = re.findall(r"[a-z']+", text.lower())
    return bool(words) and set(words) <= _REPEAT_WORDS and bool(_REPEAT_KEYS & set(words))


def _not_an_answer(call: RealtimeCall, item_id: str, kind: str = "noise") -> None:
    """Heard nothing (noise) or a repeat request - the question's answer is still to come
    (or is the next turn on it, if that already arrived)."""
    call.item_kind[item_id] = kind
    n = call.item_question[item_id]
    progress = _progress(call, n)
    if kind == "repeat_request":
        progress["repeat_requests"] += 1
    if progress["first_item"] == item_id:
        progress["first_item"] = next((i for i in call.items if call.item_question.get(i) == n
                                       and call.item_kind.get(i) == "follow_up"), None)
        if progress["first_item"]:
            call.item_kind[progress["first_item"]] = "answer"


def _version(q, version: str):
    """The wording + rubric Milo asked: the question itself or its easier version."""
    return q.easier if version == EASIER and q.easier else q


def _tool_output(call: RealtimeCall, name: str, n: int | None, easier: bool = False) -> tuple[dict, int]:
    """Runs one question tool; returns (output for the model, max tokens for the reply it triggers)."""
    questions = call.state.config.questions
    if n is None or not 1 <= n <= len(questions):
        if name == "ask_question" and call.current >= len(questions):
            return {"error": "There are no more questions. Call end_session now."}, MAX_REPLY_TOKENS
        return {"error": f"question_number must be between 1 and {len(questions)}."}, MAX_REPLY_TOKENS
    q = questions[n - 1]
    progress = _progress(call, n)
    if name == "ask_question":
        call.current = n
        if progress["first_item"] is None:  # never switch the rubric under an answer already given
            progress["version"] = EASIER if easier and q.easier else STANDARD
        return {"question_number": n, "say": _version(q, progress["version"]).spoken_text}, MAX_QUESTION_TOKENS
    # give_hint - from the version asked
    hints = _version(q, progress["version"]).hints
    if progress["hints_used"] >= min(MAX_HINTS, len(hints)):
        # nudges used up: the student's last turn (e.g. "I don't know") stands as the answer
        return {"no_hints_left": True,
                "say_instead": "Kindly say \"That's okay, we'll work on this in your homework\", "
                               "then go to the next question."}, MAX_REPLY_TOKENS
    _request_turn(call, n, "hint_request")
    progress["hints_used"] += 1
    return {"hint": hints[progress["hints_used"] - 1]}, MAX_REPLY_TOKENS


def _said(spoken: str, text: str) -> bool:
    """Milo already read `text` out (most of its words are in what he said)."""
    words = re.findall(r"\w+", text.lower())
    heard = set(re.findall(r"\w+", spoken.lower()))
    return bool(words) and sum(w in heard for w in words) >= 0.8 * len(words)


async def _questions_done(call: RealtimeCall, ws, outputs: list[dict]) -> None:
    """response.done with prepared questions: answer the question tools, or force
    the end once the last question has its answer and Milo just talked on."""
    calls = [o for o in outputs if o.get("type") == "function_call"
             and o.get("name") in ("ask_question", "give_hint")]
    messages = [o["id"] for o in outputs if o.get("type") == "message" and o.get("id")]
    spoken = " ".join(call.items.get(m, {}).get("text", "") for m in messages)
    max_tokens, reply_needed = MAX_REPLY_TOKENS, False
    for fc in calls:
        try:
            args = json.loads(fc.get("arguments") or "{}")
            n = int(args.get("question_number"))
        except (ValueError, TypeError, AttributeError):
            args, n = {}, None
        output, tokens = _tool_output(call, fc["name"], n, easier=args.get("easier") is True)
        if fc["name"] == "ask_question" and "say" in output and _said(spoken, output["say"]):
            for m in messages:  # the question was spoken in this same response - it belongs to it
                call.item_question[m] = n
        else:
            # the tool's text hasn't been said yet (a filler like "Okay, got it."
            # may have been) - Milo needs a reply to say it
            reply_needed = True
            max_tokens = max(max_tokens, tokens)
        await ws.send(json.dumps({
            "type": "conversation.item.create",
            "item": {"type": "function_call_output", "call_id": fc.get("call_id"), "output": json.dumps(output)},
        }))
    if reply_needed:
        await ws.send(json.dumps({"type": "response.create", "response": {"max_output_tokens": max_tokens}}))
    elif (messages and not calls and not call.forcing_end and call.current == len(call.state.config.questions)
          and _progress(call, call.current)["first_item"] is not None and not spoken.rstrip().endswith("?")):
        # Milo acknowledged the final answer but didn't end - make it (the goodbye follows).
        # A reply ending in "?" is a follow-up probe ("Can you tell me why?") - let the student answer it.
        call.forcing_end = call.forced_end_sent = True
        await ws.send(json.dumps({
            "type": "response.create",
            "response": {
                "instructions": "The student has answered the final question. Call end_session now, without speaking.",
                "tool_choice": {"type": "function", "name": "end_session"},
            },
        }))


async def _hangup(call_id: str, key: str) -> None:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(f"{_OPENAI}/calls/{call_id}/hangup", headers={"Authorization": f"Bearer {key}"})
    except httpx.HTTPError:
        pass


def transcript_turns(call: RealtimeCall) -> list[dict]:
    """[{role, content, text, question_number, kind?}] - content is the original key, text the C3 one."""
    turns = []
    for item_id, i in call.items.items():
        if not i["text"]:
            continue
        turn = {"role": i["role"], "content": i["text"], "text": i["text"],
                "question_number": call.item_question.get(item_id)}
        if item_id in call.item_kind:
            turn["kind"] = call.item_kind[item_id]
        turns.append(turn)
    return turns


def _transcript_text(turns: list[dict]) -> str:
    return "\n".join(
        (f"[Q{t['question_number']}] " if t["question_number"] else "")
        + f"[{'CO-LEARNER' if t['role'] == 'assistant' else 'STUDENT'}]"
        + (f" ({t['kind'].replace('_', ' ')})" if t.get("kind") not in (None, "answer") else "")
        + f": {t['content']}"
        for t in turns
    )


def question_answers(call: RealtimeCall) -> list[dict]:
    answers = []
    for n, q in enumerate(call.state.config.questions or [], 1):
        p = _progress(call, n)
        item = call.items.get(p["first_item"]) if p["first_item"] else None
        answers.append({
            "question_id": q.id, "question_number": n,
            "first_answer": (item["text"] or None) if item else None,
            "hints_used": p["hints_used"], "explanation_requested": p["explanation_requested"],
            "version": p["version"], "repeat_requests": p["repeat_requests"], "silences": p["silences"],
        })
    return answers


def build_rubric(questions, answers: dict) -> list[dict] | None:
    """Per-question rubric for the grader, from the version each question was asked in.
    answers: question number -> {hints_used, version} (call progress or a stored finish payload)."""
    rubric = []
    for n, q in enumerate(questions or [], 1):
        a = answers.get(n) or {}
        version = EASIER if a.get("version") == EASIER and q.easier else STANDARD
        v = _version(q, version)
        rubric.append({"question_number": n, "bloom_level": q.bloom_level, "question": v.spoken_text,
                       "key_points": v.expected_points, "misconceptions": v.misconceptions,
                       "hints_used": a.get("hints_used") or 0, "version": version})
    return rubric or None


def question_rubric(call: RealtimeCall) -> list[dict] | None:
    return build_rubric(call.state.config.questions, call.progress)


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
        answers = question_answers(call)
        if call.state.config.questions:
            answered = sum(1 for a in answers if a["first_answer"])
        else:
            answered = max(student_answers - 1, 0)  # first student turn is the "ready?" reply
        report = (await grade_transcript(call.state, _transcript_text(turns), rubric=question_rubric(call))
                  if student_answers else None)
        call.report = {
            "report": report,
            "answers": answers,
            "transcript": turns,
            "turns": turns,  # alias kept for older callers
            "answered": answered,
            "model_completed": call.state.is_completed,
            # report's own LLM call is already in total_cost_usd (grade_transcript records it)
            "cost": _cost(call),
        }
    return call.report


def _cost(call: RealtimeCall) -> dict:
    return realtime_cost_report(
        call.usage, REALTIME_MODEL, TRANSCRIBE_MODEL,
        report_cost_usd=call.state.total_cost_usd,
        duration_seconds=((call.ended_at or datetime.now()) - call.state.created_at).total_seconds(),
    )
