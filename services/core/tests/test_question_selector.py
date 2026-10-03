"""services/question_selector.py - slot planning and bank selection."""

import uuid

import pytest

from src.domain.models import (
    BloomLevel, Chapter, Dataset, DatasetQuestion, HomeworkDifficulty, QuestionType, Role, Subject, Topic, User,
    UserStatus,
)
from src.domain.models.curriculum import Subtopic
from src.domain.models.question_paper import PaperDifficultyLevel as D
from src.services.question_selector import Slot, plan_slots, select_questions

B = BloomLevel


def test_plan_slots_splits_counts_or_percentages_the_same_way():
    assert plan_slots(4, {B.REMEMBER: 2, B.APPLY: 2}, HomeworkDifficulty.EASY) == \
        plan_slots(4, {B.REMEMBER: 50, B.APPLY: 50}, HomeworkDifficulty.EASY)
    slots = plan_slots(5, {B.REMEMBER: 1, B.APPLY: 1, B.ANALYZE: 1}, HomeworkDifficulty.HARD)
    assert len(slots) == 5 and all(s.difficulty == D.HARD for s in slots)
    assert {s.bloom_level for s in slots} == {B.REMEMBER, B.APPLY, B.ANALYZE}


def test_plan_slots_mixed_spreads_hard_across_bloom_levels():
    slots = plan_slots(10, {B.REMEMBER: 30, B.UNDERSTAND: 30, B.APPLY: 40}, HomeworkDifficulty.MIXED)
    assert [s.difficulty for s in slots].count(D.HARD) == 2
    assert len({s.bloom_level for s in slots if s.difficulty == D.HARD}) == 2  # not all on one level
    assert sum(s.bloom_level == B.APPLY for s in slots) == 4


def test_plan_slots_mixed_ramps_easy_to_hard_and_defaults_to_understand():
    slots = plan_slots(5, {B.APPLY: 1}, HomeworkDifficulty.MIXED)
    assert [s.difficulty for s in slots] == [D.EASY, D.EASY, D.MEDIUM, D.MEDIUM, D.HARD]
    assert plan_slots(2, {}, HomeworkDifficulty.EASY) == [Slot(B.UNDERSTAND, D.EASY)] * 2


async def _seed(db_session) -> dict:
    subject = Subject(name=f"Science-{uuid.uuid4()}")
    user = User(firebase_uid=f"u-{uuid.uuid4()}", email=f"{uuid.uuid4()}@example.com", display_name="Admin",
                role=Role.SUPER_ADMIN, status=UserStatus.ACTIVE)
    db_session.add_all([subject, user])
    await db_session.flush()
    chapter = Chapter(subject_id=subject.id, name="Electricity", order_index=11, grade=10)
    db_session.add(chapter)
    await db_session.flush()
    t_ohm, t_series = Topic(chapter_id=chapter.id, name="11.4 Ohm's law"), Topic(chapter_id=chapter.id, name="11.6 Resistors")
    db_session.add_all([t_ohm, t_series])
    await db_session.flush()
    s_series = Subtopic(topic_id=t_series.id, name="11.6.1 Series")
    ds_imp = Dataset(name="Important", question_count=0, description="")
    ds_pyq = Dataset(name="PYQ", question_count=0, description="")
    db_session.add_all([s_series, ds_imp, ds_pyq])
    await db_session.flush()

    def q(uid, dataset, bloom, difficulty, topic=None, subtopic=None, **kw):
        return DatasetQuestion(dataset_id=dataset.id, chapter_id=chapter.id, topic_id=topic and topic.id,
                               subtopic_id=subtopic and subtopic.id, bloom_level=bloom, difficulty=difficulty,
                               question_type=QuestionType.SHORT_ANSWER, text=uid, answer="a", created_by=user.id,
                               external_uid=uid, **kw)

    db_session.add_all([
        q("series-apply-medium", ds_imp, B.APPLY, D.MEDIUM, t_series, s_series),
        q("series-apply-medium", ds_pyq, B.APPLY, D.MEDIUM, t_series, s_series),  # same question, 2 datasets
        q("series-apply-easy", ds_imp, B.APPLY, D.EASY, t_series, s_series),
        q("topic-apply-medium", ds_imp, B.APPLY, D.MEDIUM, t_series),              # topic intro, no subtopic
        q("ohm-apply-medium", ds_imp, B.APPLY, D.MEDIUM, t_ohm),                   # other topic, same chapter
        q("series-remember-easy", ds_pyq, B.REMEMBER, D.EASY, t_series, s_series),  # PYQ only
        q("series-remember-hard", ds_imp, B.REMEMBER, D.HARD, t_series, s_series),
    ])
    await db_session.flush()
    return {"chapter": chapter, "topic": t_series, "subtopic": s_series, "imp": ds_imp, "pyq": ds_pyq}


def _uids(selection):
    return [q.external_uid for _, q in selection.picked]


@pytest.mark.asyncio
async def test_prefers_the_subtopic_then_widens_to_topic_then_chapter(db_session):
    s = await _seed(db_session)
    selection = await select_questions(
        db_session, dataset_ids=[s["imp"].id], chapter_id=s["chapter"].id, topic_id=s["topic"].id,
        subtopic_id=s["subtopic"].id, slots=[Slot(B.APPLY, D.MEDIUM)] * 4, seed=1)
    assert _uids(selection) == ["series-apply-medium", "series-apply-easy", "topic-apply-medium", "ohm-apply-medium"]
    assert selection.unfilled == []


@pytest.mark.asyncio
async def test_only_enabled_datasets_are_used_and_a_shared_question_counts_once(db_session):
    s = await _seed(db_session)
    both = await select_questions(
        db_session, dataset_ids=[s["imp"].id, s["pyq"].id], chapter_id=s["chapter"].id,
        subtopic_id=s["subtopic"].id, topic_id=s["topic"].id, slots=[Slot(B.APPLY, D.MEDIUM)] * 5)
    assert _uids(both).count("series-apply-medium") == 1
    imp_only = await select_questions(
        db_session, dataset_ids=[s["imp"].id], chapter_id=s["chapter"].id, slots=[Slot(B.REMEMBER, D.EASY)])
    assert "series-remember-easy" not in _uids(imp_only)  # PYQ-only question stays out


@pytest.mark.asyncio
async def test_seen_questions_and_far_off_difficulty_leave_slots_unfilled(db_session):
    s = await _seed(db_session)
    selection = await select_questions(
        db_session, dataset_ids=[s["imp"].id, s["pyq"].id], chapter_id=s["chapter"].id,
        slots=[Slot(B.REMEMBER, D.EASY), Slot(B.REMEMBER, D.EASY)], exclude_uids={"series-remember-easy"})
    # the only other REMEMBER question is HARD - two steps from EASY, so not used
    assert selection.picked == [] and len(selection.unfilled) == 2
    assert (await select_questions(db_session, dataset_ids=[], chapter_id=s["chapter"].id,
                                   slots=[Slot(B.APPLY, D.EASY)])).unfilled == [Slot(B.APPLY, D.EASY)]


@pytest.mark.asyncio
async def test_off_difficulty_is_only_accepted_near_the_gap(db_session):
    s = await _seed(db_session)
    # HARD APPLY slot: the subtopic's MEDIUM question is one step off -> accepted
    near = await select_questions(
        db_session, dataset_ids=[s["imp"].id], chapter_id=s["chapter"].id, topic_id=s["topic"].id,
        subtopic_id=s["subtopic"].id, slots=[Slot(B.APPLY, D.HARD)])
    assert _uids(near) == ["series-apply-medium"]
    # same slot with no gap scope: only chapter-wide MEDIUMs exist -> not worth it, generate instead
    far = await select_questions(db_session, dataset_ids=[s["imp"].id], chapter_id=s["chapter"].id,
                                 slots=[Slot(B.APPLY, D.HARD)])
    assert far.picked == [] and far.unfilled == [Slot(B.APPLY, D.HARD)]
