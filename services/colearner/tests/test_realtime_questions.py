import asyncio
import json

import pytest
from pydantic import ValidationError

from src.api.routes.realtime import CreateCallIn, FinishCallIn
from src.domain.schemas import SessionConfig
from src.services import realtime_service as rs
from test_realtime_limits import MESSAGE, FakeWS, _done, _run_sideband


def _q(n, **kw):
    return {"id": f"q{n}", "order": n, "bloom_level": "Understanding", "spoken_text": f"Question {n} text?",
            "expected_points": [f"point {n}a", f"point {n}b"], "misconceptions": [f"wrong {n}"],
            "hints": [f"hint {n}.1", f"hint {n}.2"], **kw}


def _config(n=3):
    # given out of order on purpose - asked by `order`
    return SessionConfig(questions=[_q(i) for i in reversed(range(1, n + 1))])


def _user(item_id, text=None):
    events = [{"type": "conversation.item.added",
               "item": {"id": item_id, "type": "message", "role": "user", "content": [{"type": "input_audio"}]}}]
    if text is not None:
        events.append({"type": "conversation.item.input_audio_transcription.completed",
                       "item_id": item_id, "transcript": text})
    return events


def _assistant(item_id, text):
    return [{"type": "conversation.item.added", "item": {"id": item_id, "type": "message", "role": "assistant"}},
            {"type": "response.output_audio_transcript.done", "item_id": item_id, "transcript": text},
            _done({"type": "message", "id": item_id})]


def _tool(name, n, call_id):
    return _done({"type": "function_call", "name": name, "call_id": call_id,
                  "arguments": json.dumps({"question_number": n})})


def _outputs(ws):
    return {m["item"]["call_id"]: json.loads(m["item"]["output"])
            for m in ws.sent if m.get("item", {}).get("type") == "function_call_output"}


def _session_events():
    return [
        *_assistant("a0", "Hi! Electricity today. Are you ready?"),
        *_user("u0", "Yes"),
        _tool("ask_question", 1, "c1"),
        *_assistant("a1", "Question 1 text?"),
        *_user("u1", "Current is flow of charge"),
        *_assistant("a1b", "Okay, got it."),
        *_user("u1b", "Can you explain it?"),
        _tool("give_explanation", 1, "c2"),
        *_assistant("a1c", "Sure, point 1a and point 1b."),
        _tool("ask_question", 2, "c3"),
        *_assistant("a2", "Question 2 text?"),
        *_user("u2a", "Hint please"), _tool("give_hint", 2, "h1"), *_assistant("a2h1", "hint 2.1"),
        *_user("u2b", "One more hint"), _tool("give_hint", 2, "h2"), *_assistant("a2h2", "hint 2.2"),
        *_user("u2c", "Another hint?"), _tool("give_hint", 2, "h3"), *_assistant("a2h3", "Try it yourself!"),
        *_user("u2d", "Resistance goes up"),
        *_assistant("a2b", "Okay."),
        _tool("ask_question", 3, "c4"),
        *_assistant("a3", "Question 3 text?"),
        *_user("u3", "Heat is produced"),
        *_assistant("a3b", "Got it."),  # final answer acknowledged, no end_session -> forced
        _done({"type": "function_call", "name": "end_session", "call_id": "e1"}),
        *_assistant("bye", "That concludes all questions for our session."),
    ]


@pytest.fixture
def short_call(monkeypatch):
    monkeypatch.setattr(rs, "MAX_CALL_SECONDS", 0.2)
    monkeypatch.setattr(rs, "_GRACE_SECONDS", 0.05)


def test_session_uses_prepared_questions_and_tools():
    session = rs.build_session(_config())
    assert [t["name"] for t in session["tools"]] == ["ask_question", "give_hint", "give_explanation", "end_session"]
    text = session["instructions"]
    assert text.index("Question 1 text?") < text.index("Question 2 text?") < text.index("Question 3 text?")
    assert "Are you ready?" in text and "Indian English" in text


def test_fallback_without_questions_keeps_textbook_prompt():
    config = SessionConfig(textbook_context="Ohm's law V = IR")
    session = rs.build_session(config)
    assert [t["name"] for t in session["tools"]] == ["end_session"]
    assert "Ohm's law V = IR" in session["instructions"]
    assert config.num_questions == 2


def test_tool_events_map_answers_hints_and_explanations(monkeypatch, short_call):
    ws = FakeWS(_session_events())
    call, _ = _run_sideband(monkeypatch, ws, _config())

    out = _outputs(ws)
    assert out["c1"] == {"question_number": 1, "say": "Question 1 text?"}
    assert out["c2"] == {"key_points": ["point 1a", "point 1b"]}
    assert out["h1"] == {"hint": "hint 2.1"} and out["h2"] == {"hint": "hint 2.2"}
    assert out["h3"]["no_hints_left"] is True  # cap: 3rd request -> encourage instead
    # each tool-only response is followed by our response.create; question replies get the longer cap
    creates = [m["response"]["max_output_tokens"] for m in ws.sent
               if m["type"] == "response.create" and "max_output_tokens" in m.get("response", {})
               and "instructions" not in m["response"]]
    assert creates == [rs.MAX_QUESTION_TOKENS, rs.MAX_EXPLANATION_TOKENS, rs.MAX_QUESTION_TOKENS,
                       rs.MAX_REPLY_TOKENS, rs.MAX_REPLY_TOKENS, rs.MAX_REPLY_TOKENS, rs.MAX_QUESTION_TOKENS]
    forced = [m for m in ws.sent if m.get("response", {}).get("tool_choice") == {"type": "function", "name": "end_session"}]
    assert len(forced) == 1 and call.state.is_completed
    assert "response.cancel" not in [m["type"] for m in ws.sent]

    p = call.progress
    assert p[1] == {"first_item": "u1", "hints_used": 0, "explanation_requested": True}
    assert p[2] == {"first_item": "u2d", "hints_used": 2, "explanation_requested": False}
    assert p[3]["first_item"] == "u3"
    assert call.item_kind["u1b"] == "explanation_request" and call.item_kind["u2c"] == "hint_request"


def test_finish_payload_and_rubric_passed_to_grader(monkeypatch, short_call):
    graded = {}

    async def fake_grade(session, transcript, rubric=None, client=None):
        graded.update(transcript=transcript, rubric=rubric)
        return {"session_summary": {"overall_mastery_percent": 70}, "qa_transcripts": []}

    monkeypatch.setattr(rs, "grade_transcript", fake_grade)
    call, _ = _run_sideband(monkeypatch, FakeWS(_session_events()), _config())
    monkeypatch.setitem(rs._calls, call.call_id, call)
    result = asyncio.run(rs.finish_call(call.call_id, "u1"))

    assert set(result) >= {"report", "answers", "transcript", "turns", "cost", "answered", "model_completed"}
    assert result["answers"] == [
        {"question_id": "q1", "question_number": 1, "first_answer": "Current is flow of charge",
         "hints_used": 0, "explanation_requested": True},
        {"question_id": "q2", "question_number": 2, "first_answer": "Resistance goes up",
         "hints_used": 2, "explanation_requested": False},
        {"question_id": "q3", "question_number": 3, "first_answer": "Heat is produced",
         "hints_used": 0, "explanation_requested": False},
    ]
    assert result["answered"] == 3 and result["model_completed"] is True
    assert result["turns"] is result["transcript"]
    by_text = {t["text"]: t for t in result["transcript"]}
    assert by_text["Yes"]["question_number"] is None
    assert by_text["Question 2 text?"]["question_number"] == 2
    assert by_text["Hint please"] == {"role": "user", "content": "Hint please", "text": "Hint please",
                                      "question_number": 2, "kind": "hint_request"}
    assert by_text["Resistance goes up"]["kind"] == "answer"

    rubric = graded["rubric"]
    assert [r["question_number"] for r in rubric] == [1, 2, 3]
    assert rubric[1]["key_points"] == ["point 2a", "point 2b"] and rubric[1]["misconceptions"] == ["wrong 2"]
    assert rubric[1]["hints_used"] == 2
    assert "[Q2] [STUDENT] (hint request): Hint please" in graded["transcript"]
    assert "[Q2] [STUDENT]: Resistance goes up" in graded["transcript"]
    assert result["report"]["session_summary"]["overall_mastery_percent"] == 70


def test_finish_without_questions_keeps_old_shape(monkeypatch, short_call):
    graded = {}

    async def fake_grade(session, transcript, rubric=None, client=None):
        graded.update(rubric=rubric)
        return {"qa_transcripts": []}

    monkeypatch.setattr(rs, "grade_transcript", fake_grade)
    events = [*_assistant("a0", "Ready?"), *_user("u0", "Yes"), *_assistant("a1", "Q?"), *_user("u1", "Answer")]
    call, _ = _run_sideband(monkeypatch, FakeWS(events), SessionConfig())
    monkeypatch.setitem(rs._calls, call.call_id, call)
    result = asyncio.run(rs.finish_call(call.call_id, "u1"))
    assert graded["rubric"] is None and result["answers"] == [] and result["answered"] == 1
    assert result["turns"][-1] == {"role": "user", "content": "Answer", "text": "Answer", "question_number": None}


def test_ask_beyond_last_question_says_end():
    call = rs.RealtimeCall(call_id="c", owner_ref="o", state=rs.session_store.create_session(_config(2)))
    call.current = 2
    assert "end_session" in rs._tool_output(call, "ask_question", 3)[0]["error"]


def test_request_limits():
    with pytest.raises(ValidationError):
        SessionConfig(questions=[_q(1)] * 11)
    with pytest.raises(ValidationError):
        SessionConfig(questions=[_q(1, spoken_text="x" * 1001)])
    assert _config(4).num_questions == 4 and [q.order for q in _config(4).questions] == [1, 2, 3, 4]
    with pytest.raises(ValidationError):
        CreateCallIn(sdp="x" * 20_001, config=SessionConfig(), owner_ref="s1")
    with pytest.raises(ValidationError):
        CreateCallIn(sdp="v=0", config=SessionConfig(), owner_ref="x" * 129)
    with pytest.raises(ValidationError):
        FinishCallIn(owner_ref="")
