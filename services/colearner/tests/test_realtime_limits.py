import asyncio
import json
import logging

import pytest

from src.domain.schemas import SessionConfig
from src.services import realtime_service as rs
from src.services.session_store import session_store


class FakeWS:
    """Feeds scripted server events, records what we send, then idles like a live call."""

    def __init__(self, events):
        self.events = [json.dumps(e) for e in events]
        self.sent = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.events:
            return self.events.pop(0)
        await asyncio.Event().wait()  # no more events: block like an idle call

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _run_sideband(monkeypatch, ws, config=None):
    hangups = []

    async def fake_hangup(call_id, key):
        hangups.append(call_id)

    monkeypatch.setattr(rs, "ws_connect", lambda *a, **k: ws)
    monkeypatch.setattr(rs, "_hangup", fake_hangup)
    call = rs.RealtimeCall(call_id="call_1", owner_ref="u1",
                           state=session_store.create_session(config or SessionConfig(num_questions=10)))
    asyncio.run(asyncio.wait_for(rs._sideband(call, "test-key"), timeout=5))
    return call, hangups


def _short_clock(monkeypatch, seconds):
    """A test clock of `seconds`, running from connect, and short graces."""
    monkeypatch.setattr(rs, "MAX_CALL_SECONDS", seconds)
    monkeypatch.setattr(rs, "READY_ALLOWANCE_SECONDS", 0)
    monkeypatch.setattr(rs, "_GRACE_SECONDS", 0.05)
    monkeypatch.setattr(rs, "WRAP_UP_SECONDS", 0)  # a test clock is all "last minute"
    monkeypatch.setattr(rs, "FINISH_ANSWER_SECONDS", 0.5)


def _done(*outputs):
    return {"type": "response.done", "response": {"output": list(outputs)}}


MESSAGE = {"type": "message"}


def test_session_config_caps_output_tokens():
    session = rs.build_session(SessionConfig(questions=[
        {"id": "q1", "order": 1, "bloom_level": "Understanding", "spoken_text": "Short one?"}]))
    assert session["max_output_tokens"] == rs.MAX_REPLY_TOKENS
    # 220 tokens (~10 s) cut off the ~14-15 s replies we saw; 2 sentences now fit, a runaway stays bounded
    assert 17 * 15 < rs.MAX_REPLY_TOKENS <= 17 * 40


def test_reply_cap_fits_the_longest_question_plus_context():
    longest = ("Why does a bulb glow brighter when connected in parallel? " * 20)[:1000]  # the schema max
    config = SessionConfig(questions=[
        {"id": "q1", "order": 1, "bloom_level": "Understanding", "spoken_text": "Short one?"},
        {"id": "q2", "order": 2, "bloom_level": "Understanding", "spoken_text": "Short two?", "easier": {"spoken_text": longest}},
    ])
    cap = rs.reply_tokens(config)
    assert rs.build_session(config)["max_output_tokens"] == cap
    # ~17 audio tokens per second of slow (~12 chars/s) speech + the text, for the question and a context sentence
    assert cap >= (1000 + 150) / 12 * 17 + (1000 + 150) / 4
    assert cap <= 17 * 150  # still a bound: ~2.5 min of speech
    assert rs.reply_tokens(SessionConfig()) == rs.MAX_REPLY_TOKENS


def test_time_limit_clamped_to_8_minutes():
    assert rs.call_time_limit(SessionConfig(session_max_time_seconds=3600)) == 480
    assert rs.call_time_limit(SessionConfig(session_max_time_seconds=300)) == 300


def test_time_clamp_applies_to_live_call(monkeypatch):
    _short_clock(monkeypatch, 0.05)
    ws = FakeWS([])
    _, hangups = _run_sideband(monkeypatch, ws, SessionConfig(session_max_time_seconds=3600))
    assert any(m.get("item", {}).get("content", [{}])[0].get("text") == "[TIME_UP]" for m in ws.sent)
    assert hangups == ["call_1"]


def test_reply_cap_forces_wrap_up_and_hangs_up(monkeypatch, caplog):
    monkeypatch.setattr(rs, "_GRACE_SECONDS", 0.05)  # the hang-up backstop when the app doesn't finish the call
    cap = rs.reply_cap(10)  # the default session in _run_sideband has 10 questions
    events = [_done(MESSAGE) for _ in range(cap)] + [
        {"type": "response.created"},  # the reply past the cap starts -> cancelled
        _done(),  # cancelled reply -> forced end_session
        _done({"type": "function_call", "name": "end_session", "call_id": "fc_1"}),  # -> over, no goodbye
    ]
    ws = FakeWS(events)
    with caplog.at_level(logging.INFO, logger=rs.logger.name):
        call, hangups = _run_sideband(monkeypatch, ws)

    kinds = [m["type"] for m in ws.sent]
    assert "response.cancel" in kinds
    forced = [m for m in ws.sent if m.get("response", {}).get("tool_choice") == {"type": "function", "name": "end_session"}]
    assert len(forced) == 1
    # the app hangs up at end_session: no spoken goodbye is asked for (paid audio nobody would hear)
    assert not [m for m in ws.sent[ws.sent.index(forced[0]) + 1:] if m.get("type") == "response.create"]
    assert call.state.is_completed and hangups == ["call_1"]
    cost_log = next(r for r in caplog.records if r.getMessage().startswith("realtime_call_cost"))
    assert cost_log.call_id == "call_1" and cost_log.replies == cap
    assert cost_log.total_usd == 0 and cost_log.duration_seconds >= 0


def test_forced_wrap_up_never_cuts_off_the_reply_still_playing(monkeypatch):
    # Milo's last reply is still playing when the student's turn starts the reply past the cap:
    # only that new reply is cancelled - the playing audio is never cleared.
    events = [_done(MESSAGE) for _ in range(rs.reply_cap(10))] + [
        {"type": "output_audio_buffer.started"},
        {"type": "response.created"},
    ]
    _short_clock(monkeypatch, 0.05)
    ws = FakeWS(events)
    call, _ = _run_sideband(monkeypatch, ws)
    kinds = [m["type"] for m in ws.sent]
    assert call.forcing_end and "response.cancel" in kinds
    assert "output_audio_buffer.clear" not in kinds


def test_clock_starts_at_the_first_question_not_at_connect(monkeypatch):
    monkeypatch.setattr(rs, "READY_ALLOWANCE_SECONDS", 100)
    call = rs.RealtimeCall(call_id="c", owner_ref="u", state=session_store.create_session(
        SessionConfig(session_max_time_seconds=300, questions=[{"id": "q1", "order": 1, "bloom_level": "Understanding", "spoken_text": "Q1?"},
                                                               {"id": "q2", "order": 2, "bloom_level": "Understanding", "spoken_text": "Q2?"}])))

    async def run():
        loop = asyncio.get_running_loop()
        # _sideband's absolute cap: the time limit plus the readiness allowance from connect
        async with asyncio.timeout(rs.call_time_limit(call.state.config) + rs.READY_ALLOWANCE_SECONDS) as call.deadline:
            connect = loop.time()
            assert call.deadline.when() == pytest.approx(connect + 400, abs=0.5)
            await asyncio.sleep(0.05)  # greeting + "yes, I'm ready"
            rs._tool_output(call, "ask_question", 1)
            started = loop.time()
            assert call.deadline.when() == pytest.approx(started + 300, abs=0.05)  # full time from question 1
            rs._tool_output(call, "ask_question", 2)
            assert call.deadline.when() == pytest.approx(started + 300, abs=0.05)  # later questions don't extend it
            # a handshake that drags past the allowance: the absolute cap still holds
            call.deadline.reschedule(connect + 400)
            await asyncio.sleep(0)
            call.deadline.reschedule(loop.time() + 10)
            rs._start_clock(call)
            assert call.deadline.when() == pytest.approx(loop.time() + 10, abs=0.05)

    asyncio.run(run())


def test_time_up_never_fires_after_the_end(monkeypatch):
    # end_session lands just before the time limit: the test clock stops, no goodbye is
    # asked for, no [TIME_UP] is ever sent, and an abandoned call is still hung up.
    _short_clock(monkeypatch, 0.2)
    monkeypatch.setattr(rs, "_GRACE_SECONDS", 0.5)
    ws = FakeWS([
        {"type": "conversation.item.created",
         "item": {"id": "u1", "type": "message", "role": "user", "content": [{"type": "input_audio"}]}},
        {"type": "conversation.item.created",
         "item": {"id": "u2", "type": "message", "role": "user", "content": [{"type": "input_audio"}]}},
        {"type": "conversation.item.created",
         "item": {"id": "u3", "type": "message", "role": "user", "content": [{"type": "input_audio"}]}},
        _done({"type": "function_call", "name": "end_session", "call_id": "e1"}),
    ])
    call, hangups = _run_sideband(monkeypatch, ws, SessionConfig(num_questions=2))
    texts = [c.get("text") for m in ws.sent for c in m.get("item", {}).get("content") or []]
    assert call.state.is_completed
    assert not [m for m in ws.sent if m.get("type") == "response.create" and m.get("response", {}).get("instructions")]
    assert "[TIME_UP]" not in texts and not call.time_up
    assert hangups == ["call_1"]  # the grace still hangs up an abandoned call
    assert (call.ended_at - call.state.created_at).total_seconds() >= 0.4


def test_no_cap_below_limit(monkeypatch):
    _short_clock(monkeypatch, 0.05)
    ws = FakeWS([_done(MESSAGE) for _ in range(rs.reply_cap(10) - 1)] + [{"type": "response.created"}])
    call, _ = _run_sideband(monkeypatch, ws)
    assert "response.cancel" not in [m["type"] for m in ws.sent] and not call.forcing_end


def test_asking_to_exit_early_is_refused_and_the_test_continues(monkeypatch):
    # end_session after only the "ready" turn: refused, the model told to carry on
    _short_clock(monkeypatch, 0.05)
    ws = FakeWS([
        {"type": "conversation.item.created",
         "item": {"id": "u1", "type": "message", "role": "user", "content": [{"type": "input_audio"}]}},
        {"type": "response.done", "response": {"output": [
            {"type": "function_call", "name": "end_session", "call_id": "c1", "arguments": "{}"}]}},
    ])
    call, hangups = _run_sideband(monkeypatch, ws, SessionConfig(num_questions=5))
    assert not call.state.is_completed
    instructions = [m["response"].get("instructions") for m in ws.sent
                    if m.get("type") == "response.create" and "response" in m]
    assert rs.END_REFUSED_INSTRUCTIONS in instructions


def test_may_end_only_when_really_over():
    call = rs.RealtimeCall(call_id="c", owner_ref="u", state=session_store.create_session(SessionConfig(num_questions=3)))
    assert not rs._may_end(call)
    call.spoken_turns = 4  # "ready" + 3 answers
    assert rs._may_end(call)
    call.spoken_turns, call.time_up = 1, True
    assert rs._may_end(call)


def test_reply_cap_leaves_room_for_follow_ups_and_nudges():
    assert rs.reply_cap(2) == rs.MAX_REPLIES  # short tests keep the floor
    assert rs.reply_cap(10) >= 5 * 10  # ask + follow-up + two nudges + acknowledgement each


def test_no_new_question_in_the_last_minute(monkeypatch):
    # with under WRAP_UP_SECONDS left Milo closes the current question instead of starting one
    call = rs.RealtimeCall(call_id="c", owner_ref="u", state=session_store.create_session(
        SessionConfig(session_max_time_seconds=300, questions=[
            {"id": f"q{n}", "order": n, "bloom_level": "Understanding", "spoken_text": f"Q{n}?"} for n in (1, 2, 3)])))

    async def run():
        loop = asyncio.get_running_loop()
        async with asyncio.timeout(300) as call.deadline:
            assert rs._tool_output(call, "ask_question", 1)["say"] == "Q1?"
            assert rs._tool_output(call, "ask_question", 2)["say"] == "Q2?"  # plenty of time left
            call.deadline.reschedule(loop.time() + rs.WRAP_UP_SECONDS - 5)
            assert "say" in rs._tool_output(call, "ask_question", 2)  # the current one can still be repeated
            assert "no more questions" in rs._tool_output(call, "ask_question", 3)["error"]
            assert call.wrapping_up and rs._may_end(call)

    asyncio.run(run())

