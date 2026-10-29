import asyncio
import json
import logging

import pytest
from pydantic import ValidationError

from src.api.routes.realtime import CreateCallIn, FinishCallIn
from src.domain.schemas import SessionConfig
from src.services import realtime_service as rs
from test_realtime_limits import MESSAGE, FakeWS, _done, _run_sideband, _short_clock


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
        *_assistant("a1b", "Can you tell me why?"),  # follow-up probe
        *_user("u1b", "Because charges move"),
        _tool("ask_question", 2, "c3"),
        *_assistant("a2", "Question 2 text?"),
        *_user("u2a", "Hint please"), _tool("give_hint", 2, "h1"), *_assistant("a2h1", "hint 2.1"),
        *_user("u2b", "I don't know"), _tool("give_hint", 2, "h2"), *_assistant("a2h2", "hint 2.2"),
        *_user("u2c", "Resistance goes up"), _tool("give_hint", 2, "h3"),  # nudges used up: this stays the answer
        *_assistant("a2h3", "That's okay, we'll work on this in your homework."),
        _tool("ask_question", 3, "c4"),
        *_assistant("a3", "Question 3 text?"),
        *_user("u3", "Heat is produced"),
        *_assistant("a3b", "Got it."),  # final answer acknowledged, no end_session -> forced
        _done({"type": "function_call", "name": "end_session", "call_id": "e1"}),
        *_assistant("bye", "That concludes all questions for our session."),
    ]


@pytest.fixture
def short_call(monkeypatch):
    _short_clock(monkeypatch, 0.2)


def test_session_uses_prepared_questions_and_tools():
    session = rs.build_session(_config())
    assert [t["name"] for t in session["tools"]] == ["ask_question", "give_hint", "end_session"]
    text = session["instructions"]
    assert text.index("Question 1 text?") < text.index("Question 2 text?") < text.index("Question 3 text?")
    assert "Are you ready?" in text and "Indian English" in text
    assert "never ask whether they are ready for the next question" in text.lower()
    assert "never say question numbers aloud" in text.lower()


def test_instructions_never_carry_the_answers():
    for config in (_config(), SessionConfig(textbook_context="Ohm's law")):
        session = rs.build_session(config)
        assert "point 1a" not in session["instructions"] and "Key points" not in session["instructions"]
        assert "give_explanation" not in json.dumps(session)
    addendum = rs.build_session(SessionConfig()).get("instructions").lower()
    assert "never ask whether they are ready for the next question" in addendum
    assert "never say question numbers aloud" in addendum


def test_speakerphone_audio_config():
    audio_in = rs.build_session(_config())["audio"]["input"]
    assert audio_in["noise_reduction"] == {"type": "far_field"}
    # speakerphone echo of Milo's own voice must never cut him off - only "Tap to interrupt" does
    assert audio_in["turn_detection"] == {"type": "semantic_vad", "eagerness": "medium",
                                          "interrupt_response": False, "create_response": True}


def test_fallback_without_questions_keeps_textbook_prompt():
    config = SessionConfig(textbook_context="Ohm's law V = IR")
    session = rs.build_session(config)
    assert [t["name"] for t in session["tools"]] == ["end_session"]
    assert "Ohm's law V = IR" in session["instructions"]
    assert config.num_questions == 2


def test_tool_events_map_answers_and_hints(monkeypatch, short_call):
    ws = FakeWS(_session_events())
    call, _ = _run_sideband(monkeypatch, ws, _config())

    out = _outputs(ws)
    assert out["c1"] == {"question_number": 1, "say": "Question 1 text?"}
    assert out["h1"] == {"hint": "hint 2.1"} and out["h2"] == {"hint": "hint 2.2"}
    assert out["h3"]["no_hints_left"] is True and "homework" in out["h3"]["say_instead"]  # cap: 3rd -> move on
    assert "point" not in json.dumps(out)  # no expected points ever reach the model
    # each tool-only response is followed by our response.create, capped to fit the longest question
    creates = [m["response"]["max_output_tokens"] for m in ws.sent
               if m["type"] == "response.create" and "max_output_tokens" in m.get("response", {})
               and "instructions" not in m["response"]]
    assert creates == [rs.reply_tokens(_config())] * 6
    forced = [m for m in ws.sent if m.get("response", {}).get("tool_choice") == {"type": "function", "name": "end_session"}]
    assert len(forced) == 1 and call.state.is_completed
    assert "response.cancel" not in [m["type"] for m in ws.sent]

    p = call.progress
    fresh = {"explanation_requested": False, "version": "standard", "repeat_requests": 0, "silences": 0}
    assert p[1] == {"first_item": "u1", "hints_used": 0, **fresh}
    assert p[2] == {"first_item": "u2c", "hints_used": 2, **fresh}
    assert p[3]["first_item"] == "u3"
    # the probe's reply stays with Q1, graded together with the first answer
    assert call.item_question["u1b"] == 1 and call.item_kind["u1b"] == "follow_up"
    assert call.item_kind["u2b"] == "hint_request" and call.item_kind["u2c"] == "answer"


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
    quality = {"explanation_requested": False, "version": "standard", "repeat_requests": 0, "silences": 0}
    assert result["answers"] == [
        {"question_id": "q1", "question_number": 1, "first_answer": "Current is flow of charge",
         "hints_used": 0, **quality},
        {"question_id": "q2", "question_number": 2, "first_answer": "Resistance goes up",
         "hints_used": 2, **quality},
        {"question_id": "q3", "question_number": 3, "first_answer": "Heat is produced",
         "hints_used": 0, **quality},
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
    assert "[Q1] [STUDENT] (follow up): Because charges move" in graded["transcript"]
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
    assert "end_session" in rs._tool_output(call, "ask_question", 3)["error"]


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


def _assistant_and_tool(item_id, text, name, n, call_id):
    """One response that both speaks and calls a question tool."""
    return [{"type": "conversation.item.added", "item": {"id": item_id, "type": "message", "role": "assistant"}},
            {"type": "response.output_audio_transcript.done", "item_id": item_id, "transcript": text},
            _done({"type": "message", "id": item_id},
                  {"type": "function_call", "name": name, "call_id": call_id,
                   "arguments": json.dumps({"question_number": n})})]


def _plain_creates(ws):
    return [m for m in ws.sent if m["type"] == "response.create" and "instructions" not in m.get("response", {})
            and "max_output_tokens" in m.get("response", {})]


def test_filler_plus_tool_in_one_response_still_gets_spoken(monkeypatch, short_call):
    events = [
        *_assistant("a0", "Hi! Are you ready?"), *_user("u0", "Yes"),
        _tool("ask_question", 1, "c1"), *_assistant("a1", "Question 1 text?"),
        *_user("u1", "Current flows"),
        *_assistant_and_tool("a1b", "Okay, got it.", "ask_question", 2, "c2"),  # question not said yet
        *_assistant("a2", "Question 2 text?"),
        *_user("u2", "Hint please"),
        *_assistant_and_tool("a2h", "Sure.", "give_hint", 2, "h1"),
        *_assistant("a2h1", "hint 2.1"),
        *_user("u2b", "Resistance"),
        *_assistant_and_tool("a2b", "Okay. Here is the next one: Question 3 text?", "ask_question", 3, "c3"),
    ]
    ws = FakeWS(events)
    call, _ = _run_sideband(monkeypatch, ws, _config())
    # c1 (tool only), c2 (filler only), h1 (filler only) need a reply; c3 was read out in the same response
    assert [m["response"]["max_output_tokens"] for m in _plain_creates(ws)] == [rs.reply_tokens(_config())] * 3
    assert call.item_question["a1b"] == 1 and call.item_question["a2"] == 2 and call.item_question["a2b"] == 3
    assert call.progress[2]["first_item"] == "u2b" and call.item_kind["u2"] == "hint_request"


def test_repeat_request_and_noise_are_not_answers(monkeypatch, short_call):
    events = [
        *_assistant("a0", "Hi! Are you ready?"), *_user("u0", "Yes"),
        _tool("ask_question", 1, "c1"), *_assistant("a1", "Question 1 text?"),
        *_user("u1r", "Sorry, can you repeat please?"),  # barged in while Milo read the question
        *_assistant("a1r", "Question 1 text?"),
        *_user("u1noise", ""),  # noise - never an answer
        *_user("u1", "Current flows"),
    ]
    call, _ = _run_sideband(monkeypatch, FakeWS(events), _config())
    assert call.item_kind["u1r"] == "repeat_request" and call.item_kind["u1noise"] == "noise"
    assert call.progress[1]["first_item"] == "u1" and call.item_kind["u1"] == "answer"


def test_late_repeat_transcript_hands_the_answer_to_the_next_turn():
    call = rs.RealtimeCall(call_id="c", owner_ref="o", state=rs.session_store.create_session(_config(2)))
    call.current = 1
    for item_id in ("u1r", "u1"):  # both heard before the first one's transcript arrived
        call.items[item_id] = {"role": "user", "text": ""}
        call.item_question[item_id] = 1
        rs._tag_user_turn(call, item_id)
    rs._not_an_answer(call, "u1r", "repeat_request")
    assert call.progress[1]["first_item"] == "u1" and call.item_kind["u1"] == "answer"


def test_repeat_request_detection():
    for text in ("What?", "Pardon?", "Sorry, say that again", "Can you repeat the question?", "wait", "I didn't hear you"):
        assert rs._repeat_request(text), text
    for text in ("", "Current flows from positive to negative", "What happens is the bulb glows", "I don't know"):
        assert not rs._repeat_request(text), text


def test_follow_up_probe_on_last_question_is_not_cut_off(monkeypatch, short_call):
    events = [
        *_assistant("a0", "Hi! Are you ready?"), *_user("u0", "Yes"),
        _tool("ask_question", 1, "c1"), *_assistant("a1", "Question 1 text?"),
        *_user("u1", "Heat"),
        *_assistant("a1p", "Can you tell me why?"),  # probe - the student gets to answer
    ]
    ws = FakeWS(events)
    call, _ = _run_sideband(monkeypatch, ws, _config(1))
    forced = [m for m in ws.sent if m.get("response", {}).get("tool_choice") == {"type": "function", "name": "end_session"}]
    assert not forced and call.progress[1]["first_item"] == "u1"


def test_unprompted_hint_does_not_relabel_previous_answer():
    call = rs.RealtimeCall(call_id="c", owner_ref="o", state=rs.session_store.create_session(_config(3)))
    call.current = 1
    call.item_question["u1"] = 1
    rs._tag_user_turn(call, "u1")
    rs._tool_output(call, "ask_question", 2)
    rs._tool_output(call, "give_hint", 2)  # no new student turn since Q1's answer
    assert call.progress[1]["first_item"] == "u1" and call.item_kind["u1"] == "answer"


_EASIER = {"spoken_text": "Easy question 2?", "expected_points": ["easy point"], "misconceptions": ["easy wrong"],
           "hints": ["easy hint 1", "easy hint 2"]}


def _easier_config():
    return SessionConfig(questions=[_q(1), _q(2, easier=_EASIER), _q(3)])


def test_easier_version_is_asked_hinted_and_graded():
    call = rs.RealtimeCall(call_id="c", owner_ref="o", state=rs.session_store.create_session(_easier_config()))
    assert rs._tool_output(call, "ask_question", 1, easier=True)["say"] == "Question 1 text?"  # none -> standard
    assert rs._tool_output(call, "ask_question", 2, easier=True)["say"] == "Easy question 2?"
    call.items["u2"] = {"role": "user", "text": "I don't know"}
    call.item_question["u2"] = 2
    rs._tag_user_turn(call, "u2")
    assert rs._tool_output(call, "give_hint", 2) == {"hint": "easy hint 1"}
    rubric = rs.question_rubric(call)
    assert [r["version"] for r in rubric] == ["standard", "easier", "standard"]
    assert rubric[1]["question"] == "Easy question 2?" and rubric[1]["key_points"] == ["easy point"]
    assert rubric[1]["misconceptions"] == ["easy wrong"] and rubric[0]["key_points"] == ["point 1a", "point 1b"]
    assert rs.question_answers(call)[1]["version"] == "easier"


def test_config_accepts_new_fields_and_old_payloads():
    old = SessionConfig(questions=[_q(1)])
    assert old.student_name is None and old.student_context is None and old.questions[0].easier is None
    new = SessionConfig(student_name="Asha", student_context="Last test: strong at Remember, found Apply hard.",
                        questions=[_q(1, easier=_EASIER)])
    assert new.questions[0].easier.hints == ["easy hint 1", "easy hint 2"]
    with pytest.raises(ValidationError):
        SessionConfig(student_context="x" * 301)
    with pytest.raises(ValidationError):
        SessionConfig(questions=[_q(1, easier={**_EASIER, "spoken_text": "x" * 1001})])
    with pytest.raises(ValidationError):
        SessionConfig(questions=[_q(1, easier={**_EASIER, "hints": ["h"] * 5})])


def test_prompt_has_the_new_rules_and_the_student_when_given():
    text = rs.build_session(SessionConfig(student_name="Asha", student_context="Last test: strong at Remember.",
                                          questions=[_q(1)]))["instructions"]
    assert '"Asha"' in text and "Last test: strong at Remember." in text
    for rule in ("easier=true", "How would you explain that to a friend?", "Tanglish", "never correct their language",
                 '"[SILENCE]"', "Take your time. Want a small hint?", "Nice start, keep going",
                 "smaller sub-question", "first question standard"):
        assert rule in text, rule
    bare = rs.build_session(_config())["instructions"]
    assert "first name is" not in bare and "LAST TEST" not in bare
    assert "easier" in rs.ASK_QUESTION_TOOL["parameters"]["properties"]


def test_prompt_carries_the_chapter_quiz_focus_when_given():
    focus = "Found Apply hard on Ohm's law (still hard after homework). Has mastered: Current."
    text = rs.build_session(SessionConfig(student_focus=focus, questions=[_q(1)]))["instructions"]
    assert focus in text and "quicker to nudge" in text and "never treat this as a reason to give answers" in text
    assert "TOPIC QUIZZES" not in rs.build_session(_config())["instructions"]
    with pytest.raises(ValidationError):
        SessionConfig(student_focus="x" * 601)


class _SlowWS(FakeWS):
    """FakeWS that also understands a {"type": "_sleep"} pause between events."""

    async def __anext__(self):
        raw = await super().__anext__()
        if json.loads(raw).get("type") == "_sleep":
            await asyncio.sleep(0.1)
            return await self.__anext__()
        return raw


_STOPPED = {"type": "output_audio_buffer.stopped"}
_SLEEP = {"type": "_sleep"}


def _silences(ws):
    return [m for m in ws.sent if m.get("item", {}).get("content", [{}])[0].get("text") == "[SILENCE]"]


@pytest.fixture
def silence_call(monkeypatch):
    monkeypatch.setattr(rs, "SILENCE_SECONDS", 0.02)
    _short_clock(monkeypatch, 1.0)


def test_silence_check_in_once_per_question_and_counted_in_payload(monkeypatch, silence_call):
    async def fake_grade(session, transcript, rubric=None, client=None):
        return {"qa_transcripts": []}

    monkeypatch.setattr(rs, "grade_transcript", fake_grade)
    events = [
        *_assistant("a0", "Hi Asha! Are you ready?"), *_user("u0", "Yes"), _STOPPED, _SLEEP,  # greeting: no check-in
        _tool("ask_question", 1, "c1"), *_assistant("a1", "Question 1 text?"), _STOPPED, _SLEEP,  # -> [SILENCE]
        *_assistant("a1s", "Take your time. Want a small hint?"), _STOPPED, _SLEEP,  # already checked in: none
        *_user("u1r", "Sorry, can you repeat?"), *_assistant("a1r", "Question 1 text?"), _STOPPED, _SLEEP,
        *_user("u1", "Current flows"),
        _done({"type": "function_call", "name": "ask_question", "call_id": "c2",
               "arguments": json.dumps({"question_number": 2, "easier": True})}),
        *_assistant("a2", "Easy question 2?"), _STOPPED, _SLEEP,  # new question -> [SILENCE] again
        *_user("u2", "Resistance"),
    ]
    ws = _SlowWS(events)
    call, _ = _run_sideband(monkeypatch, ws, _easier_config())
    assert len(_silences(ws)) == 2
    assert _outputs(ws)["c2"]["say"] == "Easy question 2?"
    monkeypatch.setitem(rs._calls, call.call_id, call)
    answers = asyncio.run(rs.finish_call(call.call_id, "u1"))["answers"]
    assert [(a["version"], a["repeat_requests"], a["silences"]) for a in answers] == [
        ("standard", 1, 1), ("easier", 0, 1), ("standard", 0, 0)]
    assert answers[0]["first_answer"] == "Current flows"


def test_no_silence_check_in_while_anyone_is_speaking(monkeypatch, silence_call):
    events = [
        *_assistant("a0", "Hi! Are you ready?"), *_user("u0", "Yes"),
        _tool("ask_question", 1, "c1"), *_assistant("a1", "Question 1 text?"),
        _STOPPED, {"type": "output_audio_buffer.started"}, _SLEEP,  # Milo talking again
        {"type": "input_audio_buffer.speech_started"}, _STOPPED, _SLEEP,  # student barged in and is still talking
    ]
    ws = _SlowWS(events)
    call, _ = _run_sideband(monkeypatch, ws, _config())
    assert not _silences(ws) and call.progress[1]["silences"] == 0
    call.student_speaking, call.state.is_completed = False, True
    assert not rs._awaiting_answer(call, 1)  # after end_session: never


def _marker(item_id, text):
    return {"type": "conversation.item.added",
            "item": {"id": item_id, "type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": text}]}}


def test_pause_holds_the_clock_up_to_the_cap(monkeypatch):
    monkeypatch.setattr(rs, "MAX_PAUSE_SECONDS", 100)
    call = rs.RealtimeCall(call_id="c", owner_ref="u", state=rs.session_store.create_session(_config()))
    paused, resumed = _marker("p", rs.PAUSED)["item"], _marker("r", rs.RESUMED)["item"]

    async def run():
        async with asyncio.timeout(10) as call.deadline:
            start = call.deadline.when()
            rs._pause_marker(call, paused)
            rs._pause_marker(call, paused)  # a repeat changes nothing
            assert call.deadline.when() == pytest.approx(start + 100)
            call.paused_at -= 30  # paused for 30 s
            rs._pause_marker(call, resumed)
            assert call.deadline.when() == pytest.approx(start + 30) and call.paused_at is None
            rs._pause_marker(call, paused)  # only 70 s of pause left
            assert call.deadline.when() == pytest.approx(start + 100)
            call.paused_at -= 500  # far past the cap: the normal limit applies to the rest
            rs._pause_marker(call, resumed)
            assert call.deadline.when() == pytest.approx(start + 100) and call.paused_seconds == pytest.approx(100)

    asyncio.run(run())
    assert call.pauses == 2


def test_pause_silences_milo_and_markers_are_never_answers(monkeypatch, silence_call):
    async def fake_grade(session, transcript, rubric=None, client=None):
        assert "PAUSED" not in transcript and "RESUMED" not in transcript
        return {"qa_transcripts": []}

    monkeypatch.setattr(rs, "grade_transcript", fake_grade)
    events = [
        *_assistant("a0", "Hi! Are you ready?"), *_user("u0", "Yes"),
        _tool("ask_question", 1, "c1"), *_assistant("a1", "Question 1 text?"),
        _marker("p1", rs.PAUSED), _STOPPED, _SLEEP,  # paused: no [SILENCE] check-in
        {"type": "response.created"},  # nothing may talk while paused
        _marker("r1", rs.RESUMED), *_assistant("a1w", "Welcome back! Question 1 text?"), _STOPPED, _SLEEP,  # re-armed
        *_user("u1", "Current flows"),
    ]
    ws = _SlowWS(events)
    call, _ = _run_sideband(monkeypatch, ws, _config())
    assert len(_silences(ws)) == 1 and call.progress[1]["silences"] == 1
    assert {"type": "response.cancel"} in ws.sent and call.paused_at is None
    monkeypatch.setitem(rs._calls, call.call_id, call)
    result = asyncio.run(rs.finish_call(call.call_id, "u1"))
    assert result["pauses"] == 1
    assert result["answers"][0]["first_answer"] == "Current flows"
    assert all(t["text"] not in (rs.PAUSED, rs.RESUMED) for t in result["transcript"])
    assert "p1" not in call.item_kind and "r1" not in call.item_kind


def test_prompts_keep_milo_quiet_while_paused():
    for config in (_config(), SessionConfig()):
        text = rs.build_session(config)["instructions"]
        assert 'If you receive "[PAUSED]", say nothing.' in text
        assert '"[RESUMED]", say one short welcome back line and repeat the current question exactly.' in text


CARD = {"steps": ["H2 + O2 -> H2O", "H2 + O2 -> H2O2", "Balanced."],
        "fixes": ["2H2 + O2 -> 2H2O", "H2 + O2 -> H2O2", "H2 + 2O2 -> H2O"]}


def _card_tap(item_id, step, fix):
    return [{"type": "conversation.item.added", "item": {
        "id": item_id, "type": "message", "role": "user",
        "content": [{"type": "input_text", "text": f"[CARD_ANSWERED step={step} fix={fix}] The student tapped."}]}}]


def test_a_fix_milo_card_is_on_screen_answered_by_a_tap_then_the_why(monkeypatch, short_call):
    config = SessionConfig(questions=[_q(1), _q(2, card=CARD), _q(3)])
    text = rs.build_session(config)["instructions"]
    assert "Q2 [Understanding] (ON SCREEN)" in text and "[CARD_ANSWERED" in text
    assert "H2O2" not in text  # the steps are on the screen, never in Milo's mouth
    assert "(ON SCREEN)" not in rs.build_session(_config())["instructions"]

    ws = FakeWS([
        *_assistant("a0", "Hi! Are you ready?"), *_user("u0", "Yes"),
        _tool("ask_question", 1, "c1"), *_assistant("a1", "Question 1 text?"), *_user("u1", "Flow of charge"),
        _tool("ask_question", 2, "c2"), *_assistant("a2", "Question 2 text?"),
        *_card_tap("t2", 1, 0),
        *_assistant("a2b", "Why was that step wrong?"), *_user("u2", "You can't change a subscript"),
        _tool("ask_question", 3, "c3"), *_assistant("a3", "Question 3 text?"), *_user("u3", "Heat"),
        *_assistant("a3b", "Got it."),
        _done({"type": "function_call", "name": "end_session", "call_id": "e1"}),
        *_assistant("bye", "That concludes all questions for our session."),
    ])
    call, _ = _run_sideband(monkeypatch, ws, config)

    # the tap is question 2's answer, written out for the grader; the spoken "why" follows it
    assert call.progress[2]["first_item"] == "t2" and call.item_kind["u2"] == "follow_up"
    assert call.items["t2"]["text"] == ('(tapped on the screen) Step 2 is the wrong one: "H2 + O2 -> H2O2". '
                                        'The fix: "2H2 + O2 -> 2H2O".')
    assert call.progress[2]["silences"] == 0  # no "take your time" while they tap
    turns = [t for t in rs.transcript_turns(call) if t["question_number"] == 2]
    assert [t["role"] for t in turns] == ["assistant", "user", "assistant", "user"]
    assert call.state.is_completed


def test_a_card_tap_out_of_range_or_off_a_card_is_ignored():
    config = SessionConfig(questions=[_q(1), _q(2, card=CARD)])
    call = rs.RealtimeCall(call_id="c", owner_ref="o", state=rs.SessionState(session_id="s", config=config))
    for current, step in ((1, 0), (2, 9)):
        call.current = current
        item = _card_tap(f"t{current}", step, 0)[0]["item"]
        call.items[item["id"]] = {"role": "user", "text": ""}
        rs._card_answer(call, item)
        assert call.items[item["id"]]["text"] == "" and call.spoken_turns == 0


def test_time_up_lets_the_student_finish_then_closes_in_one_line(monkeypatch):
    _short_clock(monkeypatch, 0.1)
    ws = _SlowWS([{"type": "input_audio_buffer.speech_started"},  # mid-answer when time runs out
                  _SLEEP, _SLEEP, {"type": "input_audio_buffer.speech_stopped"}])
    call, _ = _run_sideband(monkeypatch, ws, _config())
    time_up = next(i for i, m in enumerate(ws.sent)
                   if m.get("item", {}).get("content", [{}])[0].get("text") == "[TIME_UP]")
    assert call.time_up and not call.student_speaking  # [TIME_UP] only once the answer ended
    # the closing line: sent, or queued behind the reply still going (the greeting here)
    close = ws.sent[time_up + 1] if len(ws.sent) > time_up + 1 else json.loads(call.pending_response)
    assert close["type"] == "response.create" and "one short" in close["response"]["instructions"]


def test_a_wrong_card_tap_gets_no_why_milo_goes_on():
    config = SessionConfig(questions=[_q(1), _q(2, card={**CARD, "wrong_step": 1}), _q(3)])
    call = rs.RealtimeCall(call_id="c", owner_ref="o", state=rs.SessionState(session_id="s", config=config))
    call.current = 2

    class WS:
        sent = []

        async def send(self, raw):
            self.sent.append(json.loads(raw))

    async def run():
        ws = WS()
        for event in (*_card_tap("t2", 0, 0), {"type": "response.created"},  # the app's "ask why" reply
                      {"type": "response.done", "response": {"output": []}}):
            await rs._handle_event(call, ws, event)
        return ws.sent

    sent = asyncio.run(run())
    assert sent[0] == {"type": "response.cancel"}
    assert sent[1]["type"] == "response.create" and "Don't ask why" in sent[1]["response"]["instructions"]
    assert "wrong_step" not in rs.build_session(config)["instructions"]
    # the right step still gets its "why?"
    call.items["t3"] = {"role": "user", "text": ""}
    rs._card_answer(call, _card_tap("t3", 1, 0)[0]["item"])
    assert call.redirect is None


class _FakeOpenAI:
    """httpx.AsyncClient stand-in for POST /calls: hands out call_1, call_2, ..."""
    made = 0

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, *a, **k):
        _FakeOpenAI.made += 1
        return type("R", (), {"status_code": 201, "text": "answer-sdp",
                              "headers": {"Location": f"/v1/realtime/calls/call_{_FakeOpenAI.made}"}})()


@pytest.fixture
def openai(monkeypatch):
    """create_call against fakes: each call's sideband plays the next scripted FakeWS."""
    _FakeOpenAI.made = 0
    rs._calls.clear()
    sockets, hangups = [], []

    async def fake_hangup(call_id, key):
        hangups.append(call_id)

    monkeypatch.setattr(rs.httpx, "AsyncClient", _FakeOpenAI)
    monkeypatch.setattr(rs, "_api_key", lambda: "k")
    monkeypatch.setattr(rs, "_hangup", fake_hangup)
    monkeypatch.setattr(rs, "ws_connect", lambda *a, **k: sockets.pop(0))
    yield sockets, hangups
    rs._calls.clear()


_FIRST_TWO = [*_assistant("a0", "Hi! Are you ready?"), *_user("u0", "Yes"),
              _tool("ask_question", 1, "c1"), *_assistant("a1", "Question 1 text?"),
              *_user("u1", "Current is flow of charge"),
              _tool("ask_question", 2, "c2"), *_assistant("a2", "Question 2 text?")]


async def _open(config, uid="stu"):
    _, call = await rs.create_call(sdp_offer="o", config=config, owner_ref="s1", owner_uid=uid)
    return call


async def _stop(*calls):
    for call in calls:
        if call.task and not call.task.done():
            call.task.cancel()
        await call.ended.wait()


def test_a_reconnect_resumes_the_same_test_with_its_answers_and_clock(monkeypatch, openai):
    """V-001: the app reloaded on question 2 - the new call carries on from there: Q1's
    answer kept for grading, the clock less what was used, and no second greeting.
    Time away is held like a pause, only up to what is left of the pause allowance."""
    sockets, hangups = openai
    monkeypatch.setattr(rs, "MAX_PAUSE_SECONDS", 0.05)
    second = FakeWS([])
    sockets += [FakeWS(_FIRST_TWO), second]

    async def scenario():
        old = await _open(_config())
        await asyncio.sleep(0.05)  # Q1 answered, Q2 asked - then the page goes away
        await _stop(old)
        await asyncio.sleep(0.2)  # away 0.2 s: 0.05 held (the allowance), the rest on the clock
        new = await _open(_config())
        await asyncio.sleep(0.05)
        left = rs._seconds_left(new)
        await _stop(new)
        return old, new, left

    old, new, left = asyncio.run(scenario())
    assert hangups[0] == "call_1" and "call_1" not in rs._calls  # the old call is gone for good
    assert new.resumed and new.state is old.state and new.current == 2
    assert new.paused_seconds == pytest.approx(0.05)  # the allowance is used up, not reset
    assert 0.15 < new.clock_used < 0.5  # time away past the allowance ran the clock
    limit = rs.call_time_limit(new.state.config)
    assert limit - 1 < left <= limit - new.clock_used
    opening = second.sent[0]["response"]["instructions"]
    assert "Don't greet again" in opening and "call ask_question with 2" in opening
    answers = rs.question_answers(new)
    assert answers[0]["first_answer"] == "Current is flow of charge"  # Q1 kept for grading
    assert answers[1]["first_answer"] is None


def test_another_user_cannot_resume_someone_elses_call(openai):
    """The session id alone is not enough: only the student who started the call resumes it."""
    sockets, hangups = openai
    sockets += [FakeWS(_FIRST_TWO), FakeWS([])]

    async def scenario():
        victim = await _open(_config())
        await asyncio.sleep(0.05)
        other = await _open(_config(), uid="intruder")
        untouched = not victim.ended.is_set() and rs._calls.get("call_1") is victim
        await _stop(victim, other)
        return victim, other, untouched

    victim, other, untouched = asyncio.run(scenario())
    assert untouched  # the victim's call kept running, still theirs
    assert not other.resumed and other.state is not victim.state and other.current == 0


def test_a_call_being_graded_is_never_resumed(monkeypatch, openai):
    """A reload on the "checking your answers" screen: finish has started - a new call
    starts apart, and the graded session keeps its answers and is recorded once."""
    sockets, _ = openai
    sockets += [FakeWS(_FIRST_TWO), FakeWS([])]

    async def scenario():
        release = asyncio.Event()

        async def slow_grade(*a, **k):
            await release.wait()
            return {"overall": 1}

        monkeypatch.setattr(rs, "grade_transcript", slow_grade)
        old = await _open(_config())
        await asyncio.sleep(0.05)
        finishing = asyncio.create_task(rs.finish_call(old.call_id, "s1"))
        await asyncio.sleep(0.05)  # grading in progress
        new = await _open(_config())
        release.set()
        result = await finishing
        await _stop(new)
        return old, new, result

    old, new, result = asyncio.run(scenario())
    assert not new.resumed and new.state is not old.state
    assert result["answers"][0]["first_answer"] == "Current is flow of charge"


def test_an_improvised_test_resumes_too_never_two_calls_side_by_side(openai):
    """No prepared questions (prep failed): the old call is still hung up and dropped."""
    sockets, hangups = openai
    second = FakeWS([])
    sockets += [FakeWS([*_assistant("a0", "Hi! Are you ready?"), *_user("u0", "Yes")]), second]
    config = SessionConfig(textbook_context="Ohm's law")

    async def scenario():
        old = await _open(config)
        await asyncio.sleep(0.05)
        new = await _open(config)
        await asyncio.sleep(0.05)
        await _stop(new)
        return old, new

    old, new = asyncio.run(scenario())
    assert new.resumed and new.state is old.state and new.spoken_turns == 1
    assert hangups[0] == "call_1" and list(rs._calls) == ["call_2"]
    opening = second.sent[0]["response"]["instructions"]
    assert "Ohm's law" in opening and "carry on the test where it stopped" in opening


def test_cost_lines_of_a_resumed_session_add_up_to_its_total(monkeypatch, openai, caplog):
    """Each hang-up logs only its own call's share (scripts/cost_report.py sums the lines)."""
    sockets, _ = openai
    sockets += [FakeWS(_FIRST_TWO), FakeWS([])]
    monkeypatch.setattr(rs, "_cost", lambda call: {"total_usd": round(sum(call.usage.values()), 6),
                                                   "duration_seconds": 1})

    async def scenario():
        old = await _open(_config())
        await asyncio.sleep(0.05)
        old.usage["fake_usd"] = 0.10
        new = await _open(_config())
        new.usage["fake_usd"] += 0.05  # shared usage: the session's total is now 0.15
        await asyncio.sleep(0.05)
        await _stop(new)

    with caplog.at_level(logging.INFO, logger=rs.logger.name):
        asyncio.run(scenario())
    logged = [r.total_usd for r in caplog.records if r.getMessage().startswith("realtime_call_cost")]
    assert logged == [0.1, 0.05]
