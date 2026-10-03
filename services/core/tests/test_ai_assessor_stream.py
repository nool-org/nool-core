"""Tests stream_session directly as a plain coroutine - a fake WebSocket +
fake colearner session + monkeypatched get_current_user/get_by_firebase_uid/
get_session sidesteps needing a real network socket, real Postgres, or a
real colearner instance, while still exercising the real translation logic
between colearner's turn-based protocol and the AiAssessorEvent WS
contract (see ai_assessor.py's stream_session and
src/services/colearner_client.py's module docstring for why Core bridges
rather than pointing the app at colearner directly).

Every scenario here uses two bloom_levels (REMEMBER, UNDERSTAND), so
num_questions = max(len(bloom_levels), 2) = 2 - the FakeColearnerSession
scripts below are written for exactly that shape.
"""

from types import SimpleNamespace

from starlette.websockets import WebSocketDisconnect

from shared.auth import AuthenticatedUser
from src.api.routes import ai_assessor
from src.domain.models import BloomLevel


class FakeWebSocket:
    def __init__(self, incoming: list[dict]):
        self.sent: list[dict] = []
        self._incoming = list(incoming)
        self.closed = False

    async def accept(self) -> None:
        pass

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)

    async def receive_json(self) -> dict:
        if not self._incoming:
            raise WebSocketDisconnect()
        return self._incoming.pop(0)

    async def close(self, code: int = 1000) -> None:
        self.closed = True


class FakeDbSession:
    def __init__(self, ai_session, bloom_levels):
        self._ai_session = ai_session
        self._bloom_levels = bloom_levels
        self.calls = 0

    async def execute(self, _query):
        self.calls += 1
        if self.calls == 1:
            return SimpleNamespace(scalar_one_or_none=lambda: self._ai_session)
        return SimpleNamespace(all=lambda: [(lvl,) for lvl in self._bloom_levels])

    async def get(self, *_args, **_kwargs):
        # Only reached when app_user.school_id or ai_session.test_id is
        # truthy - every test here uses a school-less fake user and
        # test_id=None (turn-taking is independent of school/VoiceTest
        # resolution, covered separately), so this is never actually hit.
        return None


class FakeSessionContext:
    def __init__(self, session: FakeDbSession):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False


class FakeColearnerSession:
    """Scripted colearner responses, returned in order: the first from
    send_config, the rest from successive send_turn calls.
    """

    def __init__(self, turns: list[dict]):
        self._turns = list(turns)
        self.sent_turns: list[dict] = []
        self.closed = False
        # Set by a test that cares whether the client was already told
        # 'thinking' before this (mocked, here-instant, but really a
        # multi-second STT+LLM call) round trip even started.
        self.ws_sent_ref: list[dict] | None = None
        self.ws_sent_snapshots_at_call: list[list[dict]] = []

    async def send_config(self, config: dict) -> dict:
        return self._turns.pop(0)

    def _snapshot_ws_sent(self) -> None:
        if self.ws_sent_ref is not None:
            self.ws_sent_snapshots_at_call.append(list(self.ws_sent_ref))

    async def send_turn(self, *, text=None, audio_base64=None, time_limit_reached=False) -> dict:
        self._snapshot_ws_sent()
        self.sent_turns.append(
            {"text": text, "audio_base64": audio_base64, "time_limit_reached": time_limit_reached}
        )
        return self._turns.pop(0)

    async def send_hint_request(self) -> dict:
        self._snapshot_ws_sent()
        self.sent_turns.append({"is_hint_request": True})
        return self._turns.pop(0)

    async def close(self) -> None:
        self.closed = True


def _wire(monkeypatch, ai_session, bloom_levels, colearner_turns: list[dict] | None = None):
    monkeypatch.setattr(
        ai_assessor,
        "get_current_user",
        lambda authorization: AuthenticatedUser(uid="uid-1", email=None, claims={}),
    )
    monkeypatch.setattr(
        ai_assessor, "get_by_firebase_uid", _async_returning(SimpleNamespace(id="student-1", school_id=None))
    )
    fake_db = FakeDbSession(ai_session, bloom_levels)
    monkeypatch.setattr(ai_assessor, "get_session", lambda: FakeSessionContext(fake_db))

    fake_colearner = FakeColearnerSession(colearner_turns or [])

    async def fake_connect(*, pipeline, token):
        return fake_colearner

    monkeypatch.setattr(ai_assessor, "ColearnerSession", SimpleNamespace(connect=fake_connect))
    return fake_colearner


def _async_returning(value):
    async def _fn(*_args, **_kwargs):
        return value

    return _fn


def _session(duration_seconds: int = 60, *, test_id=None, student_id="student-1"):
    # test_id=None by default so these turn-taking tests never touch
    # record_test_completion - that's covered on its own, against a real
    # db_session, in test_test_completion.py.
    return SimpleNamespace(
        id="sess-1", context_label="Science", duration_seconds=duration_seconds,
        test_id=test_id, student_id=student_id,
    )


_GREETING = {
    "is_completed": False, "turn_index": 1, "student_transcript": None,
    "co_learner_text": "Hi! Are you ready to begin?", "co_learner_audio_base64": None,
}
_Q1 = {
    "is_completed": False, "turn_index": 2, "student_transcript": "yes",
    "co_learner_text": "Question 1?", "co_learner_audio_base64": None,
}
_Q2 = {
    "is_completed": False, "turn_index": 3, "student_transcript": "answer1",
    "co_learner_text": "Question 2?", "co_learner_audio_base64": None,
}
_FINAL = {
    "is_completed": True, "turn_index": 4, "student_transcript": "answer2",
    "co_learner_text": "", "co_learner_audio_base64": None,
    "report": {"qa_transcripts": []},
}
_HINT_REPLY = {
    "is_completed": False, "turn_index": 2, "student_transcript": None,
    "co_learner_text": "Think about what happens to current when the path narrows.",
    "co_learner_audio_base64": None, "is_hint_reply": True,
}


async def test_advances_only_after_a_message_frame(monkeypatch):
    ai_session = _session()
    _wire(monkeypatch, ai_session, [BloomLevel.REMEMBER, BloomLevel.UNDERSTAND], [_GREETING, _Q1, _Q2, _FINAL])

    ws = FakeWebSocket([{"type": "message", "text": "answer"} for _ in range(3)])
    await ai_assessor.stream_session(ws, "sess-1", token="good")

    questions = [f for f in ws.sent if f["type"] == "question"]
    # 2 real questions - the greeting (Turn 0's readiness check) is no
    # longer sent as a QuestionEvent (see stream_session's
    # is_first_assessor_turn handling).
    assert len(questions) == 2
    assert questions[0]["question"]["total"] == 2
    assert questions[0]["question"]["index"] == 0
    assert questions[0]["question"]["bloomLevel"] == "REMEMBER"
    assert questions[1]["question"]["bloomLevel"] == "UNDERSTAND"
    assert "type" not in questions[0]["question"]
    assert _event_types(ws.sent)[-1] == "completed"
    assert ws.sent[-1]["summary"]["questionsAnswered"] == 2


def _event_types(sent: list[dict]) -> list[str]:
    return [frame["type"] for frame in sent]


async def test_readiness_ack_is_not_counted_as_an_answered_question(monkeypatch):
    ai_session = _session()
    _wire(monkeypatch, ai_session, [BloomLevel.REMEMBER, BloomLevel.UNDERSTAND], [_GREETING, _Q1, _Q2, _FINAL])

    ws = FakeWebSocket([{"type": "message", "text": "answer"} for _ in range(3)])
    await ai_assessor.stream_session(ws, "sess-1", token="good")

    progress_events = [f for f in ws.sent if f["type"] == "progress"]
    # Exactly 2 ProgressEvents (after answer1 and answer2), not 3 - the
    # readiness ack ("yes") produces a TranscriptEvent but no
    # ProgressEvent/answered increment.
    assert [p["answered"] for p in progress_events] == [1, 2]


async def test_ignores_non_message_frames_while_waiting(monkeypatch):
    ai_session = _session()
    _wire(monkeypatch, ai_session, [BloomLevel.REMEMBER, BloomLevel.UNDERSTAND], [_GREETING, _Q1, _Q2, _FINAL])

    # audio/pause frames shouldn't count as an answer or advance the turn.
    ws = FakeWebSocket(
        [{"type": "audio"}, {"type": "pause"}, {"type": "message", "text": "answer"}]
        + [{"type": "message", "text": "answer"} for _ in range(2)]
    )
    await ai_assessor.stream_session(ws, "sess-1", token="good")

    assert len([f for f in ws.sent if f["type"] == "question"]) == 2
    assert ws.sent[-1]["type"] == "completed"


async def test_stop_frame_ends_the_session_early(monkeypatch):
    ai_session = _session()
    fake_colearner = _wire(monkeypatch, ai_session, [BloomLevel.REMEMBER, BloomLevel.UNDERSTAND], [_GREETING, _Q1])

    ws = FakeWebSocket([{"type": "message", "text": "yes"}, {"type": "stop"}])
    await ai_assessor.stream_session(ws, "sess-1", token="good")

    assert len([f for f in ws.sent if f["type"] == "question"]) == 1
    assert ws.sent[-1]["type"] == "exited"
    assert fake_colearner.closed


async def test_times_out_when_the_student_never_replies(monkeypatch):
    ai_session = _session(duration_seconds=0)
    timeout_wrapup = {
        "is_completed": True, "turn_index": 1, "student_transcript": None,
        "co_learner_text": "", "co_learner_audio_base64": None, "report": {"qa_transcripts": []},
    }
    fake_colearner = _wire(monkeypatch, ai_session, [BloomLevel.REMEMBER, BloomLevel.UNDERSTAND], [_GREETING, timeout_wrapup])

    ws = FakeWebSocket([])  # no client frames at all
    await ai_assessor.stream_session(ws, "sess-1", token="good")

    assert ws.sent[-1]["type"] == "timed_out"
    assert ws.sent[-1]["summary"]["questionsAnswered"] == 0
    # The timeout is relayed to colearner (time_limit_reached=True) so it
    # can produce a real wrap-up report, rather than Core just giving up
    # locally with no assessment at all.
    assert fake_colearner.sent_turns == [{"text": None, "audio_base64": None, "time_limit_reached": True}]


async def test_sends_thinking_before_the_real_answer_processing_wait_not_only_after(monkeypatch):
    # Regression test: turnState must flip to 'thinking' the instant the
    # student's answer is submitted, not only once colearner's (real,
    # multi-second STT+LLM) reply already came back - otherwise the client
    # sees no signal at all during that wait (mic already stopped, but
    # still shows "listening"), which reads as frozen rather than
    # "processing your answer."
    ai_session = _session()
    fake_colearner = _wire(monkeypatch, ai_session, [BloomLevel.REMEMBER, BloomLevel.UNDERSTAND], [_GREETING, _Q1, _Q2, _FINAL])

    ws = FakeWebSocket([{"type": "message", "text": "answer"} for _ in range(3)])
    fake_colearner.ws_sent_ref = ws.sent
    await ai_assessor.stream_session(ws, "sess-1", token="good")

    assert len(fake_colearner.ws_sent_snapshots_at_call) == 3
    for snapshot in fake_colearner.ws_sent_snapshots_at_call:
        assert snapshot[-1] == {"type": "turn", "state": "thinking"}


async def test_hint_request_gives_a_guiding_nudge_without_advancing_the_question(monkeypatch):
    ai_session = _session()
    fake_colearner = _wire(
        monkeypatch, ai_session, [BloomLevel.REMEMBER, BloomLevel.UNDERSTAND],
        [_GREETING, _Q1, _HINT_REPLY, _Q2, _FINAL],
    )

    ws = FakeWebSocket([
        {"type": "message", "text": "yes"},
        {"type": "hint"},
        {"type": "message", "text": "answer1"},
        {"type": "message", "text": "answer2"},
    ])
    fake_colearner.ws_sent_ref = ws.sent
    await ai_assessor.stream_session(ws, "sess-1", token="good")

    hints = [f for f in ws.sent if f["type"] == "hint"]
    assert len(hints) == 1
    assert hints[0]["text"] == _HINT_REPLY["co_learner_text"]

    # The hint didn't produce or consume a question, or count as an answer.
    assert len([f for f in ws.sent if f["type"] == "question"]) == 2
    progress_events = [f for f in ws.sent if f["type"] == "progress"]
    assert [p["answered"] for p in progress_events] == [1, 2]

    assert fake_colearner.sent_turns[1] == {"is_hint_request": True}
    assert ws.sent[-1]["type"] == "completed"

    # The hint request itself also gets an immediate 'thinking' signal,
    # same guarantee as a normal answer (see the dedicated regression test
    # above) - a hint round trip is a real LLM call too. 4 calls: "yes",
    # "hint", "answer1", "answer2".
    assert len(fake_colearner.ws_sent_snapshots_at_call) == 4
    for snapshot in fake_colearner.ws_sent_snapshots_at_call:
        assert snapshot[-1] == {"type": "turn", "state": "thinking"}


async def test_stop_frame_after_a_hint_ends_the_session(monkeypatch):
    ai_session = _session()
    fake_colearner = _wire(
        monkeypatch, ai_session, [BloomLevel.REMEMBER, BloomLevel.UNDERSTAND],
        [_GREETING, _Q1, _HINT_REPLY],
    )

    ws = FakeWebSocket([
        {"type": "message", "text": "yes"},
        {"type": "hint"},
        {"type": "stop"},
    ])
    await ai_assessor.stream_session(ws, "sess-1", token="good")

    assert ws.sent[-1]["type"] == "exited"
    assert fake_colearner.closed


async def test_unknown_session_id_closes_immediately(monkeypatch):
    _wire(monkeypatch, None, [])

    ws = FakeWebSocket([])
    await ai_assessor.stream_session(ws, "no-such-session", token="good")

    assert ws.closed
    assert ws.sent == []
