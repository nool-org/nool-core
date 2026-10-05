"""Cascaded pipeline (prototype): streaming STT -> text LLM -> streaming TTS, all OpenAI.

The Duolingo-style design: the conversation runs on a cheap text model
instead of a speech-to-speech model, so the expensive part (Milo's voice)
is plain TTS. Same session flow and prompt as chained; different models,
and every stage is timed so cost and latency can be compared with realtime.

ponytail: one recorded clip per turn, so STT time here includes transcribing
the whole clip. In production the mic would stream into a realtime
transcription session and the transcript would be ready ~0.3 s after the
student stops; `est_streaming_first_audio_s` reports that estimate.
"""

from __future__ import annotations

import io
import time
import wave
from datetime import datetime

from src.services.audio_service import convert_audio_to_wav
from src.services.chained_service import (
    build_chained_system_prompt,
    generate_bloom_report,
    openai_client,
)
from src.services.cost_service import USD_TO_INR_RATE
from src.services.session_store import SessionState

STT_MODEL = "gpt-4o-mini-transcribe"
LLM_MODEL = "gpt-4.1-mini"
TTS_MODEL = "gpt-4o-mini-tts"
TTS_VOICE = "coral"
TTS_INSTRUCTIONS = "Speak like a friendly Indian classmate: warm, clear, not too fast."

# USD - developers.openai.com/api/docs/pricing, checked 2026-10-05
STT_PER_MIN = 0.003
LLM_IN, LLM_IN_CACHED, LLM_OUT = 0.40, 0.10, 1.60   # per 1M tokens
TTS_PER_MIN = 0.015                                  # OpenAI's per-minute estimate for gpt-4o-mini-tts
TTS_TEXT_PER_M_CHARS = 0.60
ENDPOINTING_S = 0.3  # streaming STT: transcript final ~this long after speech ends

SHORT_TURNS = """

SPOKEN TURN RULES: every reply is at most 2 short sentences (acknowledge + next question), except when the student asks for an explanation."""


def _wav_seconds(wav: bytes) -> float:
    with wave.open(io.BytesIO(wav)) as w:
        return w.getnframes() / w.getframerate()


def _pcm_to_wav(pcm: bytes, rate: int = 24000) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return out.getvalue()


def _add(session: SessionState, usd: float) -> None:
    session.total_cost_usd += usd


async def _speak(session: SessionState, text: str) -> tuple[bytes, float, float]:
    """Streams TTS; returns (wav bytes, seconds to first audio byte, cost)."""
    t0, first, pcm = time.time(), None, bytearray()
    async with openai_client.audio.speech.with_streaming_response.create(
        model=TTS_MODEL, voice=TTS_VOICE, input=text, instructions=TTS_INSTRUCTIONS, response_format="pcm"
    ) as resp:
        async for chunk in resp.iter_bytes():
            if first is None:
                first = time.time() - t0
            pcm.extend(chunk)
    seconds = len(pcm) / 48000  # 24 kHz, 16-bit mono
    cost = seconds / 60 * TTS_PER_MIN + len(text) / 1e6 * TTS_TEXT_PER_M_CHARS
    session.cascaded["tts_usd"] += cost
    session.cascaded["tts_seconds"] += seconds
    _add(session, cost)
    return _pcm_to_wav(bytes(pcm)), first or 0.0, cost


async def _think(session: SessionState) -> tuple[str, float]:
    """Streams the LLM reply; returns (text, seconds to first token)."""
    t0, first, parts, usage = time.time(), None, [], None
    stream = await openai_client.chat.completions.create(
        model=LLM_MODEL, messages=session.openai_messages, temperature=0.5, stream=True,
        stream_options={"include_usage": True},
    )
    async for ev in stream:
        if ev.usage:
            usage = ev.usage
        if ev.choices and ev.choices[0].delta.content:
            if first is None:
                first = time.time() - t0
            parts.append(ev.choices[0].delta.content)
    text = "".join(parts).strip()
    if usage:
        cached = getattr(usage.prompt_tokens_details, "cached_tokens", 0) or 0
        cost = ((usage.prompt_tokens - cached) * LLM_IN + cached * LLM_IN_CACHED + usage.completion_tokens * LLM_OUT) / 1e6
        session.cascaded["llm_usd"] += cost
        _add(session, cost)
    return text, first or 0.0


async def _hear(session: SessionState, audio: bytes) -> tuple[str, float]:
    wav = convert_audio_to_wav(audio, 24000)
    seconds = _wav_seconds(wav) if wav[:4] == b"RIFF" else 0.0
    f = io.BytesIO(wav)
    f.name = "answer.wav"
    t0 = time.time()
    tr = await openai_client.audio.transcriptions.create(
        model=STT_MODEL, file=f, language="en",
        prompt=f"A school student answering a {session.config.subject} question about {session.config.chapter_name}.",
    )
    cost = seconds / 60 * STT_PER_MIN
    session.cascaded["stt_usd"] += cost
    session.cascaded["stt_seconds"] += seconds
    _add(session, cost)
    return tr.text.strip(), time.time() - t0


def cost_report(session: SessionState) -> dict:
    c = session.cascaded
    total = session.total_cost_usd
    return {
        "total_usd": round(total, 6),
        "total_inr": round(total * USD_TO_INR_RATE, 4),
        "breakdown_usd": {k: round(c[k], 6) for k in ("stt_usd", "llm_usd", "tts_usd", "report_usd")},
        "audio_seconds": {"student": round(c["stt_seconds"], 1), "milo": round(c["tts_seconds"], 1)},
        "models": {"stt": STT_MODEL, "llm": LLM_MODEL, "tts": TTS_MODEL},
        "prices_checked": "2026-10-05",
    }


async def process_cascaded_turn(
    session: SessionState, user_audio_bytes: bytes | None = None, user_text: str | None = None,
    time_limit_reached: bool = False, is_hint_request: bool = False,
) -> dict:
    config = session.config
    if not hasattr(session, "cascaded"):
        session.cascaded = {"stt_usd": 0.0, "llm_usd": 0.0, "tts_usd": 0.0, "report_usd": 0.0,
                            "stt_seconds": 0.0, "tts_seconds": 0.0}
    timing = {}

    if session.turn_index == 0:
        reply = (f"Hi! Welcome to our {config.subject} discussion on {config.chapter_name}. "
                 f"We'll explore {config.num_questions} quick questions together. Are you ready to begin?")
        session.openai_messages = [
            {"role": "system", "content": build_chained_system_prompt(config) + SHORT_TURNS},
            {"role": "assistant", "content": reply},
        ]
        session.turns.append({"role": "assistant", "content": reply})
        session.turn_index = 1
        wav, timing["tts_first_audio_s"], _ = await _speak(session, reply)
        return {"student_transcript": None, "text": reply, "audio": wav, "completed": False, "timing": timing}

    transcript = ""
    if is_hint_request:
        session.openai_messages.append({"role": "user", "content": (
            "[The student asked for a hint on the CURRENT question - not an answer. Give ONE short nudge; "
            "never reveal or judge the answer; stay on this question.]")})
    elif not time_limit_reached:
        if user_audio_bytes:
            transcript, timing["stt_s"] = await _hear(session, user_audio_bytes)
        else:
            transcript = (user_text or "").strip()
        transcript = transcript or "(Inaudible audio response)"
        session.turns.append({"role": "user", "content": transcript})
        session.openai_messages.append({"role": "user", "content": transcript})

    answered = sum(1 for t in session.turns if t["role"] == "user") >= config.num_questions + 1
    out_of_time = time_limit_reached or (datetime.now() - session.created_at).total_seconds() >= config.session_max_time_seconds
    if answered or out_of_time:
        session.openai_messages.append({"role": "user", "content": (
            "[The session is over. In 2 short sentences, give a warm peer summary without judging right or wrong, "
            "then end with: That concludes all questions for our session. Great job discussing these concepts today!]")})

    reply, timing["llm_first_token_s"] = await _think(session)
    session.openai_messages.append({"role": "assistant", "content": reply})
    if not is_hint_request:
        session.turns.append({"role": "assistant", "content": reply})
    session.turn_index += 1
    wav, timing["tts_first_audio_s"], _ = await _speak(session, reply)
    timing["est_streaming_first_audio_s"] = round(ENDPOINTING_S + timing["llm_first_token_s"] + timing["tts_first_audio_s"], 2)

    report = None
    if answered or out_of_time:
        session.is_completed = True
        before = session.total_cost_usd
        full = "\n".join(f"[{'CO-LEARNER' if t['role'] == 'assistant' else 'STUDENT'}]: {t['content']}"
                         for t in session.turns if t.get("content"))
        report = await generate_bloom_report(session, full)  # adds its own cost to total_cost_usd
        session.cascaded["report_usd"] += session.total_cost_usd - before
        session.report = report
    return {"student_transcript": transcript or None, "text": reply, "audio": wav, "completed": session.is_completed,
            "report": report, "timing": timing, "hint": is_hint_request}
