"""Teacher re-grade / manual score for needs-review attempts (routes/test_reviews.py)."""

import uuid

import pytest
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import select

from shared.errors import ConflictError, ForbiddenError, NotFoundError
from src.api.routes import ai_assessor, assigned_test, test_reviews, voice_test
from src.api.schemas.ai_assessor import FinishSessionIn
from src.api.schemas.test_reviews import ResolveReviewIn
from src.domain.models import (
    AiAssessorSessionRecord,
    AuditLog,
    StudentTestResult,
    StudentTestResultBloomScore,
    StudentTestResultTopicScore,
    TestStatus,
    VoiceTestTargetStudent,
)
from src.services import regrade as regrade_service
from tests.test_voice_test_questions import B, _prepared, _qa, _student_session, _teacher, _wire_finish, schema  # noqa: F401


async def _failed_attempt(db, monkeypatch):
    s = await _prepared(db, monkeypatch)
    s["test"].status = TestStatus.SCHEDULED
    student, ai_session = await _student_session(db, s)
    db.add(VoiceTestTargetStudent(test_id=s["test"].id, student_id=student.id))
    _wire_finish(monkeypatch, db, {
        "report": {"qa_transcripts": [], "session_summary": {}, "grading_error": "APITimeoutError: timed out"},
        "answers": [{"question_number": 1, "first_answer": "a", "hints_used": 1},
                    {"question_number": 2, "first_answer": "b", "hints_used": 0}],
        "transcript": [{"role": "user", "text": "a", "question_number": 1}], "answered": 2, "model_completed": True})
    await ai_assessor.finish_session(str(ai_session.id), FinishSessionIn(call_id="c1"), authorization="Bearer t",
                                     app_user=student, db=db)
    monkeypatch.setattr(regrade_service, "schedule_auto_homework", lambda **kwargs: None)
    assert s["test"].completed_count == 1
    return s, student, ai_session


def _kw(s, db, user=None):
    user = user or s["teacher"]
    return dict(user=user, _feature=user, session=db)


async def _result(db, s):
    return (await db.execute(select(StudentTestResult).where(StudentTestResult.test_id == s["test"].id))).scalar_one()


async def _audit(db, ai_session):
    return (await db.execute(select(AuditLog).where(AuditLog.target_id == str(ai_session.id)))).scalar_one()


async def _not_pending(db, s, student):
    tests = await assigned_test.list_my_tests(user=student, session=db)
    assert str(s["test"].id) not in {t.id for t in tests.items}
    reviews = await voice_test.list_test_reviews(str(s["test"].id), **_kw(s, db))
    assert reviews.items == []


async def test_regrade_records_the_result_like_a_finish_and_clears_review(schema, monkeypatch):
    db = schema
    s, student, ai_session = await _failed_attempt(db, monkeypatch)
    sent = {}

    async def fake_regrade(**kwargs):
        sent.update(kwargs)
        return {"qa_transcripts": [_qa(1, "Remembering", 80), _qa(2, "Understanding", 40)], "session_summary": {}}
    monkeypatch.setattr(regrade_service, "regrade_transcript", fake_regrade)

    out = await test_reviews.regrade_review(str(s["test"].id), str(ai_session.id), authorization="Bearer tk",
                                            **_kw(s, db))

    assert out.model_dump(by_alias=True) == {"sessionId": str(ai_session.id), "masteryPercent": 60}
    assert sent["token"] == "tk" and len(sent["config"]["questions"]) == 2
    assert sent["transcript"][0]["text"] == "a"
    result = await _result(db, s)
    assert result.mastery_percent == 60
    blooms = {b.bloom_level: b.percent for b in (await db.execute(select(StudentTestResultBloomScore).where(
        StudentTestResultBloomScore.student_test_result_id == result.id))).scalars()}
    assert blooms == {B.REMEMBER: 80, B.UNDERSTAND: 40}
    record = await db.get(AiAssessorSessionRecord, ai_session.id)
    assert record.needs_review is False and record.report["qa_transcripts"]
    assert s["test"].completed_count == 1  # not counted twice
    assert (await _audit(db, ai_session)).action == "test_review_regrade"
    await _not_pending(db, s, student)

    with pytest.raises(ConflictError):  # already cleared
        await test_reviews.regrade_review(str(s["test"].id), str(ai_session.id), authorization="Bearer tk",
                                          **_kw(s, db))


@pytest.mark.parametrize("failure", ["grading_error", "unreachable"])
async def test_regrade_failure_keeps_the_review_and_is_a_502(schema, monkeypatch, failure):
    db = schema
    s, _student, ai_session = await _failed_attempt(db, monkeypatch)

    async def fake_regrade(**kwargs):
        if failure == "unreachable":
            raise regrade_service.ColearnerUnavailableError("down")
        return {"qa_transcripts": [], "grading_error": "APITimeoutError"}
    monkeypatch.setattr(regrade_service, "regrade_transcript", fake_regrade)

    with pytest.raises(regrade_service.RegradeFailedError) as err:
        await test_reviews.regrade_review(str(s["test"].id), str(ai_session.id), authorization="Bearer t",
                                          **_kw(s, db))

    assert err.value.status_code == 502 and "try again" in err.value.message
    assert (await db.get(AiAssessorSessionRecord, ai_session.id)).needs_review is True
    assert (await db.execute(select(StudentTestResult).where(StudentTestResult.test_id == s["test"].id))).first() is None


async def test_resolve_records_a_manual_mastery_without_level_scores(schema, monkeypatch):
    db = schema
    s, student, ai_session = await _failed_attempt(db, monkeypatch)

    body = ResolveReviewIn.model_validate({"masteryPercent": 55, "note": "Heard the recording"})
    out = await test_reviews.resolve_review(str(s["test"].id), str(ai_session.id), body, **_kw(s, db))

    assert out.mastery_percent == 55
    result = await _result(db, s)
    assert result.mastery_percent == 55
    for table in (StudentTestResultBloomScore, StudentTestResultTopicScore):
        assert (await db.execute(select(table).where(table.student_test_result_id == result.id))).first() is None
    assert (await db.get(AiAssessorSessionRecord, ai_session.id)).needs_review is False
    assert s["test"].completed_count == 1
    audit = await _audit(db, ai_session)
    assert audit.action == "test_review_resolve" and audit.actor_id == s["teacher"].id
    assert audit.detail == "mastery 55%: Heard the recording"
    await _not_pending(db, s, student)


async def test_only_the_tests_teacher_and_only_a_pending_attempt(schema, monkeypatch):
    db = schema
    s, _student, ai_session = await _failed_attempt(db, monkeypatch)
    body = ResolveReviewIn(mastery_percent=50)
    stranger = await _teacher(db, s["school"].id)

    with pytest.raises(ForbiddenError):
        await test_reviews.resolve_review(str(s["test"].id), str(ai_session.id), body, **_kw(s, db, stranger))
    with pytest.raises(ForbiddenError):
        await test_reviews.regrade_review(str(s["test"].id), str(ai_session.id), authorization="Bearer t",
                                          **_kw(s, db, stranger))
    for bad in (str(uuid.uuid4()), "not-a-uuid"):
        with pytest.raises(NotFoundError):
            await test_reviews.resolve_review(str(s["test"].id), bad, body, **_kw(s, db))
    assert (await db.get(AiAssessorSessionRecord, ai_session.id)).needs_review is True
    for invalid in (-1, 101):
        with pytest.raises(PydanticValidationError):
            ResolveReviewIn(mastery_percent=invalid)
