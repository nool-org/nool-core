"""Fix Milo / Who is right? built from official board questions: which
question becomes which type, the official text is sent as the source, valid
results go into the pool as "bank" questions tied to their source, and a
source is converted only once. Generation stubbed."""

from src.domain.models import BloomLevel as B
from src.domain.models import DatasetQuestion, PracticeQuestion, QuestionType
from src.services import derived_questions as service
from tests.test_question_types import FIX, WHO
from tests.test_topic_quiz import _seed


def _q(s, topic, text, qtype, fmt, solution="", **kw):
    return DatasetQuestion(dataset_id=s.dataset.id, chapter_id=topic.chapter_id, topic_id=topic.id,
                           bloom_level=B.ANALYZE, question_type=qtype, format=fmt, text=text, answer="No",
                           solution=solution, created_by=s.teacher.id, external_uid=f"u-{text[:12]}", **kw)


def test_which_official_questions_become_which_type():
    assert service.kind_for(DatasetQuestion(format="assertion-reason", question_type=QuestionType.MCQ, text="A: x")) == "who_is_right"
    assert service.kind_for(DatasetQuestion(format="short", question_type=QuestionType.SHORT_ANSWER,
                                            text="Can 15ⁿ end with the digit 0? Give reasons.", solution="")) == "who_is_right"
    assert service.kind_for(DatasetQuestion(format="short", question_type=QuestionType.SHORT_ANSWER,
                                            text="Prove that √2 is irrational.",
                                            solution="Assume √2 = p/q in lowest terms; then p and q are both even.")) == "fix_steps"
    assert service.kind_for(DatasetQuestion(format="mcq", question_type=QuestionType.MCQ, text="HCF(6, 8) is")) is None


async def test_converts_official_questions_into_pool_questions_once(db_session, monkeypatch):
    s = await _seed(db_session)
    ar = _q(s, s.ohm, "Assertion: 6ⁿ can end with 0. Reason: ...", QuestionType.MCQ, "assertion-reason",
            options=["a", "b", "c", "d"])
    proof = _q(s, s.ohm, "Prove that √5 is irrational.", QuestionType.SHORT_ANSWER, "short",
               solution="Assume √5 = p/q in lowest terms, then 5 divides p and q - a contradiction.")
    db_session.add_all([ar, proof])
    await db_session.flush()
    sent = []

    async def special_questions(payload):
        sent.append(payload)
        out = []
        for n, slot in enumerate(payload["slots"]):
            spec = WHO if slot["kind"] == "who_is_right" else FIX
            out.append({"slot_index": n, "kind": slot["kind"], "bloom_level": slot["bloom_level"], "text": "q", "spec": spec})
        return {"questions": out}

    monkeypatch.setattr(service.generation_client, "special_questions", special_questions)
    assert await service.derive(db_session, s.electricity.id) == {"fix_steps": 1, "who_is_right": 1}
    slots = sent[0]["slots"]
    assert {sl["kind"] for sl in slots} == {"fix_steps", "who_is_right"}
    assert any(sl["source"]["solution"].startswith("Assume √5") for sl in slots)

    made = (await db_session.execute(PracticeQuestion.__table__.select().where(
        PracticeQuestion.chapter_id == s.electricity.id))).all()
    assert {(r.kind, r.origin, r.source_question_id) for r in made} == {
        ("who_is_right", "bank", ar.id), ("fix_steps", "bank", proof.id)}
    assert {r.bloom_level for r in made} == {B.ANALYZE, B.EVALUATE}

    assert await service.derive(db_session, s.electricity.id) == {"fix_steps": 0, "who_is_right": 0}  # once only
    assert len(sent) == 1
