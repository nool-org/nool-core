"""Talking over Milo (_barge_in): the student's words stop him and get an
answer; his own voice echoing back on speakerphone, or a stray "mm", doesn't."""

from src.services import realtime_service as rs
from test_realtime_limits import FakeWS, _run_sideband, _short_clock


def _over_milo(heard, milo="Great, so what happens to the current when we add a second bulb in series?"):
    return [
        {"type": "response.created", "response": {}},
        {"type": "response.output_audio_transcript.delta", "delta": milo},
        {"type": "output_audio_buffer.started"},
        {"type": "input_audio_buffer.speech_started"},
        {"type": "input_audio_buffer.speech_stopped"},
        {"type": "conversation.item.added", "item": {"id": "u1", "type": "message", "role": "user",
                                                     "content": [{"type": "input_audio"}]}},
        {"type": "conversation.item.input_audio_transcription.completed", "item_id": "u1", "transcript": heard},
    ]


def _sent_types(ws):
    return [e["type"] for e in ws.sent]


def test_echo_is_his_own_words_coming_back():
    milo = "So what happens to the current when we add a second bulb in series?"
    assert rs.is_echo("what happens to the current when we add", milo)
    assert not rs.is_echo("wait I don't understand what series means", milo)
    assert rs.is_echo("", milo)


def test_the_student_talking_over_milo_stops_him_and_gets_an_answer(monkeypatch):
    _short_clock(monkeypatch, 0.05)  # the call ends on the clock once the events run out
    ws = FakeWS(_over_milo("sorry can you say that again slower"))
    call, _ = _run_sideband(monkeypatch, ws)
    sent = _sent_types(ws)
    at = sent.index("output_audio_buffer.clear")
    assert sent[at - 1:at + 2] == ["response.cancel", "output_audio_buffer.clear", "response.create"]
    assert call.milo_speaking is False


def test_his_echo_or_a_stray_word_doesnt_cut_him_off(monkeypatch):
    _short_clock(monkeypatch, 0.05)
    for heard in ("what happens to the current when we add a second bulb", "mm"):
        ws = FakeWS(_over_milo(heard))
        _run_sideband(monkeypatch, ws)
        assert "output_audio_buffer.clear" not in _sent_types(ws), heard


def test_speaking_after_he_finished_is_an_ordinary_turn(monkeypatch):
    _short_clock(monkeypatch, 0.05)
    events = _over_milo("sorry can you say that again slower")
    events.insert(3, {"type": "output_audio_buffer.stopped"})  # he'd stopped before the student spoke
    ws = FakeWS(events)
    _run_sideband(monkeypatch, ws)
    assert "output_audio_buffer.clear" not in _sent_types(ws)
