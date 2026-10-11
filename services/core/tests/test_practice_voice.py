"""Daily practice recorded voice answers (services/practice_voice.py): the
generation/colearner clients and daily_practice's load/record are faked, the
DB session is a small in-memory stand-in - no model calls, no Postgres."""

import base64
import uuid
from types import SimpleNamespace

import pytest

import src.services as services_pkg
from shared.errors import AppError, ValidationError
from src.domain.models import BloomLevel, Chapter, DatasetQuestion, DailyPracticeItem, PracticeQuestion, Subject
from src.services import colearner_client, generation_client, practice_voice
from src.services.generation_client import GenerationServiceUnavailableError

AUDIO = base64.b64encode(b"fake-audio-bytes").decode()
SUBJECT = Subject(id=uuid.uuid4(), name="Science", grade=10)
CHAPTER = Chapter(id=uuid.uuid4(), subject_id=SUBJECT.id, name="Light", grade=10)


class FakeSession:
    def __init__(self, answers=None):
        self.rows = {SUBJECT.id: SUBJECT, CHAPTER.id: CHAPTER}
        self.answers = answers or {}
        self.commits = 0

    async def get(self, model, key):
        return self.rows.get(key)

    async def execute(self, stmt):
        entity = stmt.column_descriptions[0]["entity"]
        if entity is DatasetQuestion:
            rows = list(self.answers.items())
        else:
            rows = [CHAPTER]
        return SimpleNamespace(all=lambda: rows, scalars=lambda: SimpleNamespace(all=lambda: rows))

    async def commit(self):
        self.commits += 1


def _question(**kw):
    return PracticeQuestion(**{"id": uuid.uuid4(), "chapter_id": CHAPTER.id, "topic_id": None, "kind": "voice",
                               "bloom_level": BloomLevel.UNDERSTAND, "text": "Why does light bend in water?",
                               "expected_points": ["its speed changes"], "misconceptions": [],
                               "source_question_id": None, "explanation": None, **kw})


def _item(**kw):
    return DailyPracticeItem(**{"id": uuid.uuid4(), "kind": "voice", "answered_at": None, "xp_awarded": 0, **kw})


@pytest.fixture
def flow(monkeypatch):
    """Fakes daily_practice (written concurrently - may not exist yet) and both clients."""
    state = SimpleNamespace(item=_item(), question=_question(), recorded=[], transcribed=[], graded=[],
                            grade={"score": 80, "good": "You said the speed changes.", "add": "Add which way it bends."})

    async def load(session, session_id, item_id, user):
        assert user == "student"
        return SimpleNamespace(id=session_id), state.item, state.question

    async def record(session, item, *, score):
        state.recorded.append(score)
        return 15

    monkeypatch.setattr(services_pkg, "daily_practice",
                        SimpleNamespace(load_item_for_student=load, record_item_result=record), raising=False)

    async def transcribe(audio_base64, mime_type, prompt):
        state.transcribed.append((audio_base64, mime_type, prompt))
        return "the speed of light changes"

    async def grade(**kw):
        state.graded.append(kw)
        if isinstance(state.grade, Exception):
            raise state.grade
        return state.grade

    monkeypatch.setattr(generation_client, "transcribe", transcribe)
    monkeypatch.setattr(colearner_client, "grade_answer", grade)
    return state


async def _answer(session, audio=AUDIO):
    return await practice_voice.answer_voice_item(session, "s1", "i1", "student", audio_base64=audio,
                                                  mime_type="audio/m4a", token="tok")


async def test_answer_is_transcribed_graded_and_stored_without_the_audio(flow):
    session = FakeSession()
    out = await _answer(session)

    assert out == {"transcript": "the speed of light changes", "score": 80,
                   "feedback": {"good": "You said the speed changes.", "add": "Add which way it bends."},
                   "xp_awarded": 15}
    assert flow.recorded == [80] and session.commits == 1
    assert flow.transcribed[0][1] == "audio/m4a" and "Science. Light" in flow.transcribed[0][2]
    assert flow.graded[0]["expected_points"] == ["its speed changes"] and flow.graded[0]["subject"] == "Science"
    item = flow.item
    assert item.score == 80 and item.xp_awarded == 15 and item.answered_at is not None
    # the audio is kept nowhere on the item
    assert not any(AUDIO in str(getattr(item, c.key)) for c in DailyPracticeItem.__table__.columns)


async def test_repeat_returns_the_stored_result_without_new_calls(flow):
    await _answer(FakeSession())
    again = await _answer(FakeSession())
    assert again["score"] == 80 and len(flow.transcribed) == 1 and flow.recorded == [80]


async def test_grading_failure_keeps_the_transcript_ungraded_without_xp(flow):
    flow.grade = colearner_client.ColearnerUnavailableError("down")
    out = await _answer(FakeSession())
    assert out["transcript"] == "the speed of light changes" and out["score"] is None and out["xp_awarded"] == 0
    assert out["feedback"] == practice_voice.UNGRADED_FEEDBACK and flow.recorded == []
    assert flow.item.answered_at is not None


async def test_transcription_failure_is_a_retryable_503_and_saves_nothing(flow, monkeypatch):
    async def broken(*args):
        raise GenerationServiceUnavailableError("down")

    monkeypatch.setattr(generation_client, "transcribe", broken)
    session = FakeSession()
    with pytest.raises(AppError) as exc:
        await _answer(session)
    assert exc.value.status_code == 503
    assert flow.item.answered_at is None and session.commits == 0


async def test_bad_audio_and_mcq_items_are_refused(flow):
    with pytest.raises(ValidationError):
        await _answer(FakeSession(), audio=base64.b64encode(b"x" * (5 * 1024 * 1024 + 1)).decode())
    with pytest.raises(ValidationError):
        await _answer(FakeSession(), audio="not base64!!")
    flow.item = _item(kind="mcq")
    with pytest.raises(ValidationError):
        await _answer(FakeSession())
    assert flow.transcribed == []


def test_route_requires_a_signed_in_student(client):
    resp = client.post(f"/api/v1/me/daily-practice/{uuid.uuid4()}/items/{uuid.uuid4()}/voice",
                       json={"audioBase64": AUDIO})
    assert resp.status_code == 401


# ---- prepare_voice_questions ----------------------------------------------
async def test_prepare_batches_voice_rows_and_stores_spoken_text_and_rubric(monkeypatch):
    source_id = uuid.uuid4()
    banked = _question(text="Explain refraction.", expected_points=[], source_question_id=source_id)
    generated = _question(text="What is a lens?", expected_points=[], explanation="A curved piece of glass.")
    ready = _question()  # already has a rubric - not sent
    mcq = _question(kind="mcq", expected_points=[])
    calls = []

    async def prep(payload):
        calls.append(payload)
        return {"questions": [
            {"dataset_question_id": str(banked.id), "reworded_text": "Why does light bend?",
             "spoken_text": "Why does light bend when it enters water?", "expected_points": ["speed changes"],
             "misconceptions": ["light gets heavier"]},
            {"dataset_question_id": str(generated.id), "reworded_text": "What is a lens?",  # unchanged
             "spoken_text": "What is a lens?", "expected_points": ["curved glass"], "misconceptions": []},
        ]}

    monkeypatch.setattr(generation_client, "prep_voice_test", prep)
    await practice_voice.prepare_voice_questions(FakeSession({source_id: "Light changes speed."}),
                                                 [banked, generated, ready, mcq])

    assert len(calls) == 1
    payload = calls[0]
    assert [c["id"] for c in payload["candidates"]] == [str(banked.id), str(generated.id)]
    assert payload["candidates"][0]["answer"] == "Light changes speed."
    assert payload["candidates"][1]["answer"] == "A curved piece of glass."
    assert payload["levels"] == ["UNDERSTAND", "UNDERSTAND"] and payload["grade"] == 10
    assert payload["subject"] == "Science" and payload["chapter"] == "Light"
    assert banked.text == "Why does light bend when it enters water?"
    assert banked.expected_points == ["speed changes"] and banked.misconceptions == ["light gets heavier"]
    assert generated.text == "What is a lens?" and generated.expected_points == []  # not reworded -> left


async def test_prepare_failure_leaves_rows_untouched(monkeypatch):
    async def broken(payload):
        raise GenerationServiceUnavailableError("down")

    monkeypatch.setattr(generation_client, "prep_voice_test", broken)
    row = _question(text="Explain refraction.", expected_points=[], explanation="Bending of light.")
    await practice_voice.prepare_voice_questions(FakeSession(), [row])
    assert row.text == "Explain refraction." and row.expected_points == [] and row.misconceptions == []
