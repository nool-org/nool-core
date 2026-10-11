import asyncio
import json
import logging

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


def _done(*outputs):
    return {"type": "response.done", "response": {"output": list(outputs)}}


MESSAGE = {"type": "message"}


def test_session_config_caps_output_tokens():
    session = rs.build_session(SessionConfig())
    assert session["max_output_tokens"] == rs.MAX_REPLY_TOKENS
    assert rs.MAX_REPLY_TOKENS <= 17 * 15  # well under the ~14-15 s replies we saw


def test_time_limit_clamped_to_8_minutes():
    assert rs.call_time_limit(SessionConfig(session_max_time_seconds=3600)) == 480
    assert rs.call_time_limit(SessionConfig(session_max_time_seconds=300)) == 300


def test_time_clamp_applies_to_live_call(monkeypatch):
    monkeypatch.setattr(rs, "MAX_CALL_SECONDS", 0.05)
    monkeypatch.setattr(rs, "_GRACE_SECONDS", 0.05)
    ws = FakeWS([])
    _, hangups = _run_sideband(monkeypatch, ws, SessionConfig(session_max_time_seconds=3600))
    assert any(m.get("item", {}).get("content", [{}])[0].get("text") == "[TIME_UP]" for m in ws.sent)
    assert hangups == ["call_1"]


def test_reply_cap_forces_wrap_up_and_hangs_up(monkeypatch, caplog):
    cap = rs.reply_cap(10)  # the default session in _run_sideband has 10 questions
    events = [_done(MESSAGE) for _ in range(cap)] + [
        {"type": "response.created"},  # the reply past the cap starts -> cancelled
        _done(),  # cancelled reply -> forced end_session
        _done({"type": "function_call", "name": "end_session", "call_id": "fc_1"}),  # -> closing turn
        _done(MESSAGE),  # goodbye generated
        {"type": "output_audio_buffer.stopped"},  # goodbye played -> hang up
    ]
    ws = FakeWS(events)
    with caplog.at_level(logging.INFO, logger=rs.logger.name):
        call, hangups = _run_sideband(monkeypatch, ws)

    kinds = [m["type"] for m in ws.sent]
    assert "response.cancel" in kinds
    forced = [m for m in ws.sent if m.get("response", {}).get("tool_choice") == {"type": "function", "name": "end_session"}]
    assert len(forced) == 1
    closing = [m for m in ws.sent if m.get("response", {}).get("instructions") == rs.CLOSING_INSTRUCTIONS]
    assert closing[0]["response"]["max_output_tokens"] == rs.MAX_CLOSING_TOKENS
    assert call.state.is_completed and hangups == ["call_1"]
    cost_log = next(r for r in caplog.records if r.getMessage().startswith("realtime_call_cost"))
    assert cost_log.call_id == "call_1" and cost_log.replies == cap + 1
    assert cost_log.total_usd == 0 and cost_log.duration_seconds >= 0


def test_no_cap_below_limit(monkeypatch):
    monkeypatch.setattr(rs, "MAX_CALL_SECONDS", 0.05)
    monkeypatch.setattr(rs, "_GRACE_SECONDS", 0.05)
    ws = FakeWS([_done(MESSAGE) for _ in range(rs.reply_cap(10) - 1)] + [{"type": "response.created"}])
    call, _ = _run_sideband(monkeypatch, ws)
    assert "response.cancel" not in [m["type"] for m in ws.sent] and not call.forcing_end


def test_asking_to_exit_early_is_refused_and_the_test_continues(monkeypatch):
    # end_session after only the "ready" turn: refused, the model told to carry on
    monkeypatch.setattr(rs, "MAX_CALL_SECONDS", 0.05)
    monkeypatch.setattr(rs, "_GRACE_SECONDS", 0.05)
    ws = FakeWS([
        {"type": "conversation.item.created",
         "item": {"id": "u1", "type": "message", "role": "user", "content": [{"type": "input_audio"}]}},
        {"type": "response.done", "response": {"output": [
            {"type": "function_call", "name": "end_session", "call_id": "c1", "arguments": "{}"}]}},
    ])
    call, hangups = _run_sideband(monkeypatch, ws, SessionConfig(num_questions=5))
    assert not call.state.is_completed and not call.closing_sent
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
