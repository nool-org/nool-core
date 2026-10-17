"""Builds a homework's questions - the one path both the teacher flow
(POST /homework/{id}/generate) and the automatic post-test homework
(homework_autogen.py) go through:

1. question bank first: services/question_selector.py picks real questions
   on the test's topic from the datasets this class may use (and the
   teacher picked), skipping ones the class has already had;
2. for one student's own homework: a question generated earlier for
   another student's homework on the same topic and Bloom level, that this
   student hasn't had - each topic grows its own library of generated
   questions, so most students cost no model call (_reuse_generated);
3. generation service for the slots still open, grounded in the
   topic's own textbook text (services/generation, via generation_client);
4. if generation couldn't cover them: any unused bank question on the same
   topic (then chapter), even if its Bloom level/difficulty don't match -
   a real question beats a placeholder;
5. the existing KG/deterministic generator only for anything still missing,
   so a homework never comes back short because a service is down.
"""

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.domain.models import (
    AssignmentTargetMode, BloomLevel, Chapter, Dataset, DatasetQuestion, DatasetType, HomeworkDifficulty, HomeworkQuestion, HomeworkStatus,
    HomeworkTargetStudent, QuestionType,
    SchoolClass, Subject, Topic, User, VoiceTest,
)
from src.domain.models.curriculum import Subtopic
from src.domain.models.homework import Homework
from src.repositories.dataset_access import visible_datasets
from src.services import gap_packs, generation_client, homework_content_resolver
from src.services.question_selector import Slot, plan_slots, select_questions, text_only_filter

logger = logging.getLogger(__name__)

# Grounding text sent to the generation service - the topic and its
# subtopics, capped so one request stays well inside the model's context.
MAX_SECTION_CHARS = 12000
MAX_EXAMPLES = 4


_HANDED_OUT = (HomeworkStatus.ASSIGNED, HomeworkStatus.IN_PROGRESS, HomeworkStatus.COMPLETED)


@dataclass
class QuestionDraft:
    bloom_level: BloomLevel
    text: str
    answer: str
    dataset_id: uuid.UUID
    source_question_id: uuid.UUID | None = None  # None = generated
    source_uid: str | None = None
    is_boss: bool = False


@dataclass(frozen=True)
class Focus:
    """Where a student is weak (homework_autogen.py): the bank is searched
    on this topic/subtopic, preferring questions on these concepts.
    `bloom_level` only matters for the boss question - the slot it takes."""
    topic_id: uuid.UUID | None = None
    subtopic_id: uuid.UUID | None = None
    concepts: tuple[str, ...] = ()
    bloom_level: BloomLevel | None = None


async def build_homework_questions(
    session: AsyncSession,
    *,
    user: User,
    test: VoiceTest,
    total: int,
    bloom_weights: dict[BloomLevel, int],
    difficulty: HomeworkDifficulty,
    fallback_dataset_id: uuid.UUID,
    picked_dataset_ids: list[uuid.UUID | str] | None = None,
    seed: str | None = None,
    release_before_generation: bool = False,
    student_id: uuid.UUID | None = None,
    keep: list[QuestionDraft] = (),
    focus: Focus | None = None,
    boss: Focus | None = None,
) -> list[QuestionDraft]:
    """`picked_dataset_ids` = the teacher's choice (None = every dataset the
    user may use); `fallback_dataset_id` is recorded on generated questions
    (HomeworkQuestion.dataset_id is NOT NULL); `student_id` = the one student
    this homework is for (auto homework), whose own past homework is excluded too;
    `keep` = questions staying in this homework (a replacement mustn't repeat them);
    `focus` = the student's weak spots to target; `boss` = one slot (of the
    boss's Bloom level if the plan has one, else the hardest) goes to the
    boss's concept and comes back is_boss."""
    school_class = await session.get(SchoolClass, test.class_id)
    grade = school_class.grade if school_class else None
    if school_class is not None:  # questions already written for other students are this school's to reuse
        await gap_packs.enable_for_school(session, test.subject_id, school_class.school_id)
    visible = await visible_datasets(session, user, subject_id=test.subject_id, grade=grade)
    picked = {str(d) for d in picked_dataset_ids} if picked_dataset_ids else None
    # the teacher's picks - plus the gap pack, which only holds questions Nool already wrote for these gaps
    dataset_ids = [d.id for d in visible if d.type == DatasetType.QA
                   and (picked is None or str(d.id) in picked or d.name.startswith(gap_packs.PREFIX))]

    slots = plan_slots(total, bloom_weights, difficulty)
    seen = await _seen_uids(session, test.class_id, student_id) | {d.source_uid for d in keep if d.source_uid}
    focus = focus or Focus()
    topic_id = focus.topic_id or test.topic_id
    select_args = dict(dataset_ids=dataset_ids, chapter_id=test.chapter_id, seed=seed, text_only=True,
                       pictures=True)  # the homework screen draws them
    drafts: dict[int, QuestionDraft] = {}
    picked: list[DatasetQuestion] = []

    boss_slot = None
    if boss and slots:
        boss_slot = next((i for i, s in enumerate(slots) if s.bloom_level == boss.bloom_level), len(slots) - 1)
        hit = (await select_questions(
            session, **select_args, slots=[slots[boss_slot]], topic_id=boss.topic_id or topic_id,
            subtopic_id=boss.subtopic_id, exclude_uids=seen, focus_concepts=set(boss.concepts),
        )).by_slot[0]
        if hit is not None:
            drafts[boss_slot] = _from_bank(hit)
            picked.append(hit)
            seen = seen | {question_uid(hit)}

    rest = [i for i in range(len(slots)) if i not in drafts]
    selection = await select_questions(
        session, **select_args, slots=[slots[i] for i in rest], topic_id=topic_id, subtopic_id=focus.subtopic_id,
        exclude_uids=seen, focus_concepts=set(focus.concepts),
    )
    for i, q in zip(rest, selection.by_slot):
        if q is not None:
            drafts[i] = _from_bank(q)
            picked.append(q)
    open_slots = [i for i in range(len(slots)) if i not in drafts]
    counts = {"bank": len(drafts)}
    if open_slots and student_id and topic_id:
        await _reuse_generated(session, topic_id=topic_id, student_id=student_id, slots=slots,
                               open_slots=open_slots, drafts=drafts)
        open_slots = [i for i in range(len(slots)) if i not in drafts]
    counts["reused"] = len(drafts) - counts["bank"]

    if open_slots:
        await _generate(session, test=test, topic_id=topic_id, slots=slots, open_slots=open_slots, drafts=drafts,
                        examples=picked, fallback_dataset_id=fallback_dataset_id,
                        release_before_generation=release_before_generation, keep=keep, user=user)
    counts["generated"] = len(drafts) - counts["bank"] - counts["reused"]
    still_open = [i for i in range(len(slots)) if i not in drafts]
    if still_open:
        await _bank_relaxed_fill(session, test=test, topic_id=topic_id, dataset_ids=dataset_ids,
                                 open_slots=still_open, drafts=drafts, exclude_uids=seen)
    counts["relaxed_bank"] = len(drafts) - counts["bank"] - counts["reused"] - counts["generated"]
    still_open = [i for i in range(len(slots)) if i not in drafts]
    if still_open:
        await _legacy_fill(session, test=test, slots=slots, open_slots=still_open, drafts=drafts,
                           fallback_dataset_id=fallback_dataset_id)
    counts["legacy"] = len(drafts) - sum(counts.values())
    if boss_slot in drafts:
        drafts[boss_slot].is_boss = True
    logger.info("homework_questions_built", extra={
        "test_id": str(test.id), "requested": total, "usable_datasets": len(dataset_ids), **counts})
    return [drafts[i] for i in sorted(drafts)]


async def grounding_dataset_id(session: AsyncSession, *, user: User, test: VoiceTest,
                               picked_dataset_ids: list | None = None) -> uuid.UUID | None:
    """The dataset recorded on generated questions (HomeworkQuestion.dataset_id
    is NOT NULL): the teacher's first pick, else the first dataset this class
    may actually use for the test's subject - never some unassigned catalog
    dataset. None when the class has no usable dataset at all."""
    if picked_dataset_ids:
        return uuid.UUID(str(picked_dataset_ids[0]))
    school_class = await session.get(SchoolClass, test.class_id)
    usable = await visible_datasets(session, user, subject_id=test.subject_id,
                                    grade=school_class.grade if school_class else None)
    qa = [d for d in usable if d.type == DatasetType.QA]
    return qa[0].id if qa else None


def question_uid(q: DatasetQuestion) -> str:
    """Identity of a question across its dataset copies (the selector's key)."""
    return q.external_uid or str(q.id)


async def _reuse_generated(session: AsyncSession, *, topic_id, student_id, slots: list[Slot],
                           open_slots: list[int], drafts: dict[int, QuestionDraft]) -> None:
    """Fills open slots with questions the generation service already wrote
    for other students' homework on this topic (a topic quiz's homework has
    topic_id; a voice test's follows its test's topic) at the slot's Bloom
    level - the least handed-out first, never one this student has had or
    one already in this homework."""
    on_topic = or_(Homework.topic_id == topic_id,
                   Homework.test_id.in_(select(VoiceTest.id).where(VoiceTest.topic_id == topic_id)))
    had = select(HomeworkQuestion.text).join(
        HomeworkTargetStudent, HomeworkTargetStudent.homework_id == HomeworkQuestion.homework_id
    ).where(HomeworkTargetStudent.student_id == student_id)
    rows = (await session.execute(
        select(HomeworkQuestion.bloom_level, HomeworkQuestion.text, func.min(HomeworkQuestion.answer),
               func.min(cast(HomeworkQuestion.dataset_id, String)), func.count())
        .join(Homework, Homework.id == HomeworkQuestion.homework_id)
        .where(on_topic, HomeworkQuestion.source_question_id.is_(None), Homework.status.in_(_HANDED_OUT),
               HomeworkQuestion.text.not_in(had))
        .group_by(HomeworkQuestion.bloom_level, HomeworkQuestion.text)
        .order_by(func.count(), HomeworkQuestion.text))).all()
    taken = {d.text for d in drafts.values()}
    for i in open_slots:
        hit = next((r for r in rows if r[0] == slots[i].bloom_level and r[1] not in taken), None)
        if hit is not None:
            drafts[i] = QuestionDraft(bloom_level=hit[0], text=hit[1], answer=hit[2], dataset_id=uuid.UUID(hit[3]))
            taken.add(hit[1])


async def _seen_uids(session: AsyncSession, class_id, student_id=None) -> set[str]:
    """Questions these students were actually given: homework handed out
    (ASSIGNED onwards - not drafts in review, not failed runs) to the whole
    class, or to this one student. Another student's own auto homework
    doesn't use up the class's pool."""
    targeted = [Homework.target_mode == AssignmentTargetMode.WHOLE_CLASS]
    if student_id:
        targeted.append(Homework.id.in_(select(HomeworkTargetStudent.homework_id)
                                        .where(HomeworkTargetStudent.student_id == student_id)))
    rows = await session.execute(
        select(func.coalesce(DatasetQuestion.external_uid, cast(DatasetQuestion.id, String)))
        .join(HomeworkQuestion, HomeworkQuestion.source_question_id == DatasetQuestion.id)
        .join(Homework, Homework.id == HomeworkQuestion.homework_id)
        .where(Homework.class_id == class_id, Homework.status.in_(_HANDED_OUT), or_(*targeted))
    )
    return {uid for (uid,) in rows.all()}


def _from_bank(q: DatasetQuestion) -> QuestionDraft:
    text, answer = q.text, q.answer
    if q.options and q.question_type in (QuestionType.MCQ, QuestionType.TRUE_FALSE):
        letters = "abcdefgh"
        text += "\n" + "\n".join(f"({letters[i]}) {o}" for i, o in enumerate(q.options))
        if answer in q.options:
            answer = f"({letters[q.options.index(answer)]}) {answer}"
    if q.solution and q.solution.strip() != answer.strip():
        answer = f"{answer}\n\n{q.solution}"
    return QuestionDraft(bloom_level=q.bloom_level, text=text, answer=answer, dataset_id=q.dataset_id,
                         source_question_id=q.id, source_uid=question_uid(q))


async def _sections(session: AsyncSession, test: VoiceTest, topic_id=None) -> tuple[str, list[dict]]:
    chapter = await session.get(Chapter, test.chapter_id)
    if topic_id:
        topics = [await session.get(Topic, topic_id)]
    else:
        topics = (await session.execute(select(Topic).where(Topic.chapter_id == test.chapter_id))).scalars().all()
    sections, used = [], 0
    for topic in topics:
        subs = (await session.execute(select(Subtopic).where(Subtopic.topic_id == topic.id))).scalars().all()
        for title, text in [(topic.name, topic.text_content), *((s.name, s.text_content) for s in subs)]:
            if text and used < MAX_SECTION_CHARS:
                sections.append({"title": title, "text": text[: MAX_SECTION_CHARS - used]})
                used += len(sections[-1]["text"])
    return (chapter.name if chapter else ""), sections


async def _generate(session, *, test, topic_id, slots, open_slots, drafts, examples, fallback_dataset_id,
                    release_before_generation=False, keep=(), user=None) -> None:
    if not generation_client.is_configured():
        return
    chapter_name, sections = await _sections(session, test, topic_id)
    if not sections:
        return  # nothing to ground a question in - leave it to the legacy path
    subject = await session.get(Subject, test.subject_id)
    school_class = await session.get(SchoolClass, test.class_id)
    payload = {
        "subject": subject.name if subject else "Science",
        "grade": school_class.grade if school_class else 10,
        "chapter": chapter_name,
        "sections": sections,
        "slots": [{"bloom_level": slots[i].bloom_level.value, "difficulty": slots[i].difficulty.value} for i in open_slots],
        "examples": [{"text": q.text, "answer": q.answer[:400], "bloom_level": q.bloom_level.value,
                      "difficulty": q.difficulty.value if q.difficulty else None} for q in examples[:MAX_EXAMPLES]],
        "avoid": [d.text for d in [*keep, *drafts.values()]],
    }
    if release_before_generation:
        # end the read transaction so no connection/lock is held during the (slow) LLM call
        await session.commit()
    try:
        result = await generation_client.gap_fill(payload)
    except generation_client.GenerationServiceUnavailableError:
        return  # legacy path covers the gap
    for q in result.get("questions", []):
        slot_index = open_slots[q["slot_index"]]
        text = q["text"]
        if q.get("options"):
            text += "\n" + "\n".join(f"({'abcd'[i]}) {o}" for i, o in enumerate(q["options"][:4]))
        answer = q["answer"] if not q.get("solution") else f"{q['answer']}\n\n{q['solution']}"
        draft = QuestionDraft(bloom_level=slots[slot_index].bloom_level, text=text, answer=answer,
                              dataset_id=fallback_dataset_id)
        if user is not None and school_class is not None:
            # written once, kept in the subject's gap pack: the next student on this topic
            # and level - any class, any school - gets it from the bank
            row = await gap_packs.keep(
                session, subject_id=test.subject_id, school_id=school_class.school_id, grade=school_class.grade,
                chapter_id=test.chapter_id,
                topic_id=topic_id, created_by=user.id, bloom_level=slots[slot_index].bloom_level,
                difficulty=slots[slot_index].difficulty, text=q["text"], options=(q.get("options") or [])[:4] or None,
                answer=q["answer"], solution=q.get("solution"))
            draft.dataset_id, draft.source_question_id, draft.source_uid = row.dataset_id, row.id, row.external_uid
        drafts[slot_index] = draft


async def _bank_relaxed_fill(session, *, test, topic_id, dataset_ids, open_slots, drafts, exclude_uids) -> None:
    if not dataset_ids:
        return
    # what's already in this homework counts as used - by uid, since the same
    # question sits in several datasets under different row ids
    used_uids = set(exclude_uids) | {d.source_uid for d in drafts.values() if d.source_uid}
    query = (select(DatasetQuestion)
             .where(DatasetQuestion.dataset_id.in_(dataset_ids), DatasetQuestion.chapter_id == test.chapter_id,
                    text_only_filter(pictures=True))
             # same topic first, then the rest of the chapter
             .order_by((DatasetQuestion.topic_id == topic_id).desc().nulls_last(), DatasetQuestion.id))
    free = iter(open_slots)
    slot = next(free, None)
    for q in (await session.execute(query)).scalars():
        if slot is None:
            break
        uid = question_uid(q)
        if uid in used_uids:
            continue
        used_uids.add(uid)
        drafts[slot] = _from_bank(q)
        slot = next(free, None)


async def _legacy_fill(session, *, test, slots, open_slots, drafts, fallback_dataset_id) -> None:
    chapter = await session.get(Chapter, test.chapter_id)
    weights: dict[BloomLevel, int] = {}
    for i in open_slots:
        weights[slots[i].bloom_level] = weights.get(slots[i].bloom_level, 0) + 1
    generated = await homework_content_resolver.generate_questions(
        session, chapter_id=test.chapter_id, subject_id=test.subject_id, class_id=test.class_id,
        topic_id=test.topic_id, topic=chapter.name if chapter else "this topic",
        bloom_distribution=weights, count=len(open_slots),
    )
    pool = list(generated)
    for i in open_slots:  # each slot takes a question of its own Bloom level when there is one
        q = next((g for g in pool if g.bloom_level == slots[i].bloom_level), pool[0] if pool else None)
        if q is None:
            break
        pool.remove(q)
        drafts[i] = QuestionDraft(bloom_level=q.bloom_level, text=q.text, answer=q.answer,
                                  dataset_id=fallback_dataset_id)
