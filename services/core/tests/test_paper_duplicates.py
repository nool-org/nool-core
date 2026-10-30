"""T-070 (B26): the same question worded twice on one paper, and recall labelled Evaluate, are found and counted."""

import uuid

import pytest

from src.domain.models import BloomLevel, QuestionPaper, QuestionType
from src.domain.models.question_paper import QuestionPaperQuestion
from src.services.paper_rules import rules_check
from src.services.question_selector import duplicates_of, plain_recall, same_question
from tests.test_question_paper_generate import _seed_paper_ready_to_generate

KWH_A = ("Which unit is used by electricity boards to charge for the electrical energy used in homes?",
         ["watt", "kilowatt hour", "volt", "ampere"], "kilowatt hour")
KWH_B = ("The commercial unit of electrical energy that electricity boards use for household bills is:",
         ["joule", "kilowatt hour", "newton", "pascal"], "Kilowatt-hour")


def test_same_answer_and_concept_is_a_duplicate_but_a_different_question_is_not():
    assert same_question(KWH_A, KWH_B)
    other = ("Which device measures the electric current in a circuit?", ["ammeter", "voltmeter", "galvanometer", "fuse"], "ammeter")
    assert not same_question(KWH_A, other)
    assert duplicates_of([KWH_A, other, KWH_B]) == {2: 0}
    same_answer_other_topic = ("Which gas do green plants take in for photosynthesis in sunlight?", ["oxygen", "carbon dioxide"], "carbon dioxide")
    assert not same_question(same_answer_other_topic, ("Which gas is released when an acid reacts with a metal carbonate?", ["hydrogen", "carbon dioxide"], "carbon dioxide"))


def test_a_one_mark_fact_is_not_evaluate_but_a_judgement_can_be():
    assert plain_recall("Which of the following is the SI unit of power?", 1)
    assert not plain_recall("Which design would be the most appropriate for a low-energy aquarium?", 1)
    assert not plain_recall("Which of the following is the SI unit of power?", 3)


@pytest.mark.asyncio
async def test_rules_check_counts_repeated_and_mislabelled_questions(db_session):
    teacher, seeded = await _seed_paper_ready_to_generate(db_session)
    paper = await db_session.get(QuestionPaper, uuid.UUID(seeded["paper_id"]))
    for order, (text, options, answer) in enumerate([KWH_A, KWH_B], start=1):
        db_session.add(QuestionPaperQuestion(paper_id=paper.id, order=order, bloom_level=BloomLevel.EVALUATE, marks=1, text=text,
                                             question_type=QuestionType.MCQ, options=options, answer=answer))
    await db_session.flush()
    codes = [(i.code, i.message) for i in (await rules_check(db_session, paper)).issues]
    assert ("duplicate", "Q2 repeats Q1.") in codes
    assert [c for c, _ in codes].count("level_mismatch") == 2
