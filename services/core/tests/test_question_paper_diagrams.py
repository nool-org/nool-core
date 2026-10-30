"""B23: question papers can carry bank questions with a figure - picked on generate (a share the teacher
can switch off), offered when swapping a question, never invented by Milo."""

import random

import pytest

from src.api.routes import question_paper
from src.api.schemas.question_paper import GeneratePaperIn, UpdateQuestionPaperQuestionIn
from src.domain.models import BloomLevel, Dataset, DatasetQuestion, DatasetType, PaperDifficultyLevel, QuestionType, SchoolDataset
from tests.test_question_paper_generate import _seed_paper_ready_to_generate

SVG = '<svg viewBox="0 0 320 240"><circle cx="5" cy="5" r="3"/></svg>'


async def _seed(db_session, monkeypatch, *, ai_text="AI question {i} about algebra?"):
    teacher, seeded = await _seed_paper_ready_to_generate(db_session)
    bank = Dataset(name="PYQ", question_count=0, description="", subject_id=seeded["subject"].id,
                   board="CBSE", grade=8, type=DatasetType.QA)
    db_session.add(bank)
    await db_session.flush()
    db_session.add(SchoolDataset(school_id=teacher.school_id, dataset_id=bank.id, enabled=True))
    rng = random.Random(7)
    for level in BloomLevel:
        for difficulty in PaperDifficultyLevel:
            for figure in (None, {"svg": SVG, "role": "question", "caption": "A circle"}) * 2:  # 2 of each: slots repeat
                db_session.add(DatasetQuestion(
                    **({"figures": [figure]} if figure else {}),
                    dataset_id=bank.id, chapter_id=seeded["chapter_id"], bloom_level=level, difficulty=difficulty,
                    question_type=QuestionType.MCQ, options=["a", "b", "c", "d"], answer="a", created_by=teacher.id,
                    text=f"{'In the figure, find' if figure else 'Find'} " + " ".join(
                        "".join(rng.choices("abcdefghijklmnopqrstuvwxyz", k=7)) for _ in range(8)) + "?",))
    await db_session.flush()

    async def fake_generate_paper_questions(**kwargs):
        return [{"text": ai_text.format(i=i), "answer": "x", "bloom_level": s["bloom_level"],
                 "difficulty": s["difficulty"], "question_type": "mcq", "chapter_number": 3, "topic": "",
                 "options": ["a", "b", "c", "x"]} for i, s in enumerate(kwargs["slots"])]

    monkeypatch.setattr("src.api.routes.question_paper.kg_client.generate_paper_questions",
                        fake_generate_paper_questions)
    return teacher, seeded


async def _questions(db_session, teacher, seeded):
    return (await question_paper.list_paper_questions(seeded["paper_id"], user=teacher, session=db_session)).items


@pytest.mark.asyncio
async def test_generate_takes_a_share_of_figure_questions_inside_the_bank_share(db_session, monkeypatch):
    teacher, seeded = await _seed(db_session, monkeypatch)
    await question_paper.generate_paper(seeded["paper_id"], user=teacher, session=db_session)
    questions = await _questions(db_session, teacher, seeded)
    figured = [q for q in questions if q.figure_svg]
    assert len(questions) == 10
    assert len(figured) == question_paper.diagram_quota(10, None) == 2
    assert all(q.source == "bank" and q.figure_caption == "A circle" for q in figured)
    assert len([q for q in questions if q.source == "bank"]) == 2  # the pictures count towards the bank share
    assert len({q.text for q in questions}) == 10  # no duplicates


@pytest.mark.asyncio
async def test_diagram_questions_zero_means_no_figures_and_a_count_is_capped_at_half(db_session, monkeypatch):
    teacher, seeded = await _seed(db_session, monkeypatch)
    await question_paper.generate_paper(seeded["paper_id"], GeneratePaperIn(diagram_questions=0),
                                        user=teacher, session=db_session)
    assert not [q for q in await _questions(db_session, teacher, seeded) if q.figure_svg]
    await question_paper.generate_paper(seeded["paper_id"], GeneratePaperIn(diagram_questions=4),
                                        user=teacher, session=db_session)
    assert len([q for q in await _questions(db_session, teacher, seeded) if q.figure_svg]) == 4
    assert question_paper.diagram_quota(10, 99) == 5 and question_paper.diagram_quota(100, None) == 5


@pytest.mark.asyncio
async def test_milo_questions_that_lean_on_a_figure_are_dropped(db_session, monkeypatch):
    teacher, seeded = await _seed(db_session, monkeypatch, ai_text="As shown in the figure below, find x {i}?")
    await question_paper.generate_paper(seeded["paper_id"], GeneratePaperIn(diagram_questions=0),
                                        user=teacher, session=db_session)
    questions = await _questions(db_session, teacher, seeded)
    assert questions and all(q.source == "bank" for q in questions)  # every AI question named a missing figure


@pytest.mark.asyncio
async def test_swap_offers_figure_questions_and_swapping_one_in_brings_its_figure(db_session, monkeypatch):
    teacher, seeded = await _seed(db_session, monkeypatch)

    async def fake_candidates(**kwargs):
        return [{"text": "A plain alternative?"}, {"text": "See the diagram above and find y?"}]

    monkeypatch.setattr("src.api.routes.question_paper.kg_client.generate_replacement_candidates", fake_candidates)
    await question_paper.generate_paper(seeded["paper_id"], GeneratePaperIn(diagram_questions=0),
                                        user=teacher, session=db_session)
    target = next(q for q in await _questions(db_session, teacher, seeded) if q.source == "ai")
    candidates = (await question_paper.get_question_candidates(
        seeded["paper_id"], target.id, user=teacher, session=db_session)).items
    assert [c.text for c in candidates if not c.dataset_question_id] == ["A plain alternative?"]  # figure ref dropped
    picture = [c for c in candidates if c.figure_svg]
    assert picture and all(c.dataset_question_id and c.options for c in picture)

    after = (await question_paper.update_paper_question(
        seeded["paper_id"], target.id,
        UpdateQuestionPaperQuestionIn(text=picture[0].text, dataset_question_id=picture[0].dataset_question_id),
        user=teacher, session=db_session)).items
    swapped = next(q for q in after if q.id == target.id)
    assert (swapped.figure_svg, swapped.source, swapped.options) == (SVG, "bank", picture[0].options)

    # an AI alternative put in place of a picture question must not keep the old picture
    after = (await question_paper.update_paper_question(
        seeded["paper_id"], target.id, UpdateQuestionPaperQuestionIn(text="A plain alternative?", replace=True),
        user=teacher, session=db_session)).items
    assert next(q for q in after if q.id == target.id).figure_svg is None


@pytest.mark.asyncio
async def test_a_paper_short_of_a_question_asks_again_then_uses_the_bank_so_it_is_complete(db_session, monkeypatch):
    """T-028: kg came back one question short (11/12 questions, 20/25 marks) - generation now backfills."""
    teacher, seeded = await _seed(db_session, monkeypatch)
    calls = []

    async def short_kg(**kwargs):
        calls.append(len(kwargs["slots"]))
        return [{"text": f"Milo question {len(calls)}-{i} about algebra?", "answer": "x", "bloom_level": s["bloom_level"],
                 "difficulty": s["difficulty"], "question_type": "mcq", "chapter_number": 3, "topic": "",
                 "options": ["a", "b", "c", "x"], "slot_index": i}
                for i, s in enumerate(kwargs["slots"]) if not (len(calls) == 1 and i == 2) and not (len(calls) == 2 and True)]

    monkeypatch.setattr("src.api.routes.question_paper.kg_client.generate_paper_questions", short_kg)
    out = await question_paper.generate_paper(seeded["paper_id"], GeneratePaperIn(diagram_questions=0),
                                              user=teacher, session=db_session)
    questions = await _questions(db_session, teacher, seeded)
    assert len(questions) == 10 and len({q.text for q in questions}) == 10  # whole paper, no repeats
    assert len(calls) == 2 and calls[1] < calls[0]  # only the missing ones were asked for again
    assert out.status.value == "REVIEW" if hasattr(out.status, "value") else out.status == "REVIEW"


def test_t070_questions_that_cannot_go_on_a_paper_are_recognised():
    from src.services.question_selector import asks_too_much_for_marks, has_unusable_options, unfit_for_paper

    six_tasks = "Explain (a) what happens to R if l is doubled? (b) and Ohm's law? (c) if A is halved? (d) ...?"
    assert asks_too_much_for_marks(six_tasks, 2) and not asks_too_much_for_marks(six_tasks, 5)
    assert not asks_too_much_for_marks("State Ohm's law. Write its formula?", 2)
    assert has_unusable_options(["1/3 will be removed", "2/3 will be removed", "1/3 will be removed and 2/3 too", "none"])
    assert not has_unusable_options(["ohm", "volt", "ampere", "watt"])
    assert unfit_for_paper({"text": "Find the mean as described in the chapter."}, 3)
    assert unfit_for_paper({"text": "Use the data mentioned in the text to find x?"}, 4)
    assert not unfit_for_paper({"text": "Find the mean of 2, 4, 6."}, 1)


def test_t070_harder_difficulty_goes_to_the_bigger_questions():
    slots = question_paper._plan_slots([1, 1, 1, 1, 5, 4], [None] * 6, {}, {"EASY": 40, "MEDIUM": 40, "HARD": 20}, ["MCQ"])
    got = [(s["marks"], s["difficulty"]) for s in slots]
    assert all(s["difficulty"] != "EASY" for s in slots if s["marks"] >= 4), got
    assert got.count((5, "EASY")) == got.count((4, "EASY")) == 0
