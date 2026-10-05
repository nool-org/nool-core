import asyncio
import json
from types import SimpleNamespace

from src.domain.schemas import SessionConfig
from src.services import chained_service
from src.services.evaluation import grader
from src.services.session_store import session_store


def _answer(n, level, score, correct, hints=0):
    return {
        "question_number": n, "blooms_attribute": level, "question_asked": f"Q{n}?", "student_response": f"A{n}",
        "probing_or_hints_given": None, "points_covered": [], "misconception": None, "score_percent": score,
        "is_substantively_correct": correct, "reason": "r", "confidence": 0.9, "hints_used": hints,
    }


class _FakeClient:
    def __init__(self, payload=None, error=None):
        self.calls = []

        async def create(**kwargs):
            self.calls.append(kwargs)
            if error:
                raise error
            usage = SimpleNamespace(prompt_tokens=1_000_000, completion_tokens=1_000_000,
                                    prompt_tokens_details=SimpleNamespace(cached_tokens=1_000_000))
            msg = SimpleNamespace(content=json.dumps(payload), refusal=None)
            return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=usage)

        self.chat = SimpleNamespace(completions=SimpleNamespace(create=create))


def _grade(client, rubric=None):
    session = session_store.create_session(SessionConfig(num_questions=3))
    return session, asyncio.run(grader.grade_transcript(session, "[CO-LEARNER]: hi", rubric=rubric, client=client))


def test_aggregates_and_hint_penalty_are_computed_in_code():
    payload = {"overall_feedback": "ok", "answers": [
        _answer(1, "Understanding", 90, True),
        _answer(2, "Understanding", 70, True, hints=1),   # -10 -> 60
        _answer(3, "Applying", 15, False, hints=5),       # -20 cap -> 0 (floor)
    ]}
    client = _FakeClient(payload)
    session, report = _grade(client)

    qa = report["qa_transcripts"]
    assert [a["score_percent"] for a in qa] == [90, 60, 0]
    assert [a["raw_score_percent"] for a in qa] == [90, 70, 15]
    s = report["session_summary"]
    assert s["overall_mastery_percent"] == 50
    assert s["per_bloom"] == {"Understanding": 75, "Applying": 0}
    assert report["model"] == "gpt-5-mini" and report["prompt_version"] == grader.PROMPT_VERSION
    assert "grading_error" not in report

    call = client.calls[0]
    assert call["model"] == "gpt-5-mini"
    assert call["response_format"]["json_schema"]["strict"] is True

    # real usage: 1M uncached-billed input credited down to the cached rate, + 1M output
    assert session.turn_metrics[-1]["turn_cost_usd"] == round(0.025 + 2.00, 6)


def test_out_of_range_model_scores_are_clamped():
    payload = {"overall_feedback": "", "answers": [
        _answer(1, "Remembering", 140, True), _answer(2, "Remembering", -5, False)]}
    _, report = _grade(_FakeClient(payload))
    qa = report["qa_transcripts"]
    assert [a["raw_score_percent"] for a in qa] == [100, 0]
    assert [a["score_percent"] for a in qa] == [100, 0]


def test_report_keeps_the_shape_core_consumes():
    _, report = _grade(_FakeClient({"overall_feedback": "fine", "answers": [_answer(1, "Analyzing", 40, False)]}))
    assert {"grade", "subject", "chapter", "total_questions_asked", "overall_feedback",
            "overall_mastery_percent"} <= report["session_summary"].keys()
    assert {"question_number", "blooms_attribute", "question_asked", "student_response", "probing_or_hints_given",
            "score_percent", "is_substantively_correct"} <= report["qa_transcripts"][0].keys()
    assert report["session_summary"]["total_questions_asked"] == 3


def test_rubric_is_passed_to_the_model_when_given():
    client = _FakeClient({"overall_feedback": "", "answers": []})
    _grade(client, rubric=[{"question_number": 1, "key_points": ["V = IR"], "misconceptions": ["current is used up"]}])
    assert "current is used up" in client.calls[0]["messages"][1]["content"]


def test_failure_is_reported_not_faked():
    session, report = _grade(_FakeClient(error=RuntimeError("upstream down")))
    assert report["qa_transcripts"] == []
    assert "upstream down" in report["grading_error"]
    assert report["session_summary"]["overall_mastery_percent"] is None
    assert session.turn_metrics == []


def test_generate_bloom_report_delegates_to_grader(monkeypatch, tmp_path):
    seen = {}

    async def fake_grade(session, transcript):
        seen["t"] = transcript
        return {"session_summary": {}, "qa_transcripts": [], "grading_error": "x"}

    monkeypatch.setattr(chained_service, "grade_transcript", fake_grade)
    monkeypatch.setattr(chained_service.get_settings(), "reports_dir", tmp_path)
    session = session_store.create_session(SessionConfig(num_questions=1))
    report = asyncio.run(chained_service.generate_bloom_report(session, "TRANSCRIPT"))
    assert seen["t"] == "TRANSCRIPT" and report["grading_error"] == "x"
