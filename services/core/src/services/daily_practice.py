"""Daily practice - 5-10 minutes a day on what the teacher has taught.

- Taught topics (ClassTopicProgress) are marked by the teacher per class;
  the class's current chapter is the chapter of the most recently taught
  topic. Nothing taught -> no practice.
- Today's session (ensure_today): one per student per IST day, built on the
  first open: 6 MCQs + 2 recorded (voice) answers - 3 on the most recently
  taught topics, 2 on topics due for review, 2 on the weakest, and 1 review
  of an earlier chapter's weak/due topic (else the current chapter's).
  Never a question the student got in the last 14 days. Topics taught
  later that day rebuild an unstarted set or add up to 4 MCQs to a started
  one (twice a day at most).
- Questions come from a shared pool (PracticeQuestion): pool rows first,
  then the question bank (imported into the pool), then MCQs written by the
  generation service. MCQ explanations/hints are written once per batch and
  kept on the pool row; voice rows are prepared by practice_voice.
- Answers (answer_mcq here; recorded answers in practice_voice.py): first
  answer only. record_item_result moves the topic's mastery (moving average,
  spaced due date) and awards XP; complete() adds the completion bonus once.
"""

import logging
import re
import uuid
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from sqlalchemy import delete, exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from shared.errors import ConflictError, NotFoundError, ValidationError
from src.api.routes.gamification import apply_xp
from src.domain.models import (
    BloomLevel,
    Chapter,
    ClassTopicProgress,
    DailyPracticeItem,
    DailyPracticeSession,
    DatasetQuestion,
    DatasetType,
    Feature,
    PracticeQuestion,
    QuestionType,
    SchoolClass,
    SchoolDataset,
    StudentProfile,
    StudentTopicMastery,
    Subject,
    Topic,
    User,
)
from src.repositories.dataset_access import visible_datasets
from src.repositories.subscription_repository import get_active_plan
from src.services.feature_entitlements import is_feature_enabled
from src.services import ai_context, answer_checks, generation_client, question_types, speakable
from src.services.homework_builder import _sections
from src.services.mcq_test import (award_points, correct_index, explanation_of, shuffle_options, strip_inline_options,
                                   unlabel)
from src.services.question_selector import text_only_filter

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
NO_REPEAT_DAYS = 14
GOOD_SCORE = 70  # an answer at or above this counts as correct
INTERVAL_DAYS = [1, 3, 7, 14, 30]  # next review after 0, 1, 2, 3, 4+ good answers in a row before this one
HIGH_MASTERY = 80  # one Analyze question on a topic this strong
STRUGGLING_BELOW = 50
MCQ_XP, COMBO_FROM, COMBO_BONUS = 5, 3, 2  # +2 for the 3rd correct MCQ in a row and each one after
COMPLETION_XP = 10
VOICE_MAX_CHARS = 200  # a bank question short enough to read out and answer aloud
BANK_IMPORT_LIMIT = 10  # bank questions moved into the pool per topic and kind at a time
IDLE_DAYS, DROP_POINTS = 3, 15  # "slipping" for the teacher's summary
EXTENSION_ITEMS, MAX_EXTENSIONS, MAX_ITEMS = 4, 2, 16  # MCQs on topics taught after the day's set was built
SMALL_WORDS = {"a", "an", "and", "as", "at", "by", "for", "in", "of", "on", "or", "the", "to"}
EXTENSION_ORDER = 100  # the n-th extension's items are ordered from n*100+1: told apart without a column


def today_ist() -> date:
    return datetime.now(IST).date()


# ------------------------------------------------------------ taught topics
async def class_of(session: AsyncSession, student_id: uuid.UUID) -> SchoolClass | None:
    return (await session.execute(select(SchoolClass).join(StudentProfile, StudentProfile.class_id == SchoolClass.id)
                                  .where(StudentProfile.user_id == student_id))).scalar_one_or_none()


async def taught_topics(session: AsyncSession, class_id) -> list[tuple[ClassTopicProgress, Topic]]:
    """Most recently taught first."""
    return [tuple(r) for r in (await session.execute(
        select(ClassTopicProgress, Topic).join(Topic, Topic.id == ClassTopicProgress.topic_id)
        .where(ClassTopicProgress.class_id == class_id)
        .order_by(ClassTopicProgress.taught_on.desc(), ClassTopicProgress.created_at.desc()))).all()]


async def current_chapter(session: AsyncSession, class_id) -> Chapter | None:
    """The chapter of the most recently taught topic."""
    taught = await taught_topics(session, class_id)
    return await session.get(Chapter, taught[0][0].chapter_id) if taught else None


# ------------------------------------------------------------------ planner
@dataclass
class Slot:
    topic: Topic
    kind: str  # mcq | voice
    bloom_level: BloomLevel
    review: bool = False  # an earlier chapter's topic


def plan(taught: list[tuple[ClassTopicProgress, Topic]], mastery: dict[uuid.UUID, StudentTopicMastery],
         now: datetime) -> list[Slot]:
    """8 slots (6 mcq + 2 voice) over the taught topics: 3 recent, 2 due,
    2 weakest, 1 review. Topics repeat when there are fewer than slots."""
    chapter_id = taught[0][0].chapter_id
    current = [t for p, t in taught if p.chapter_id == chapter_id]
    earlier = [t for p, t in taught if p.chapter_id != chapter_id]
    recent = [t for p, t in taught if p.chapter_id == chapter_id and p.taught_on == taught[0][0].taught_on]

    def level(t):
        return mastery[t.id].mastery if t.id in mastery else None

    def due(t):
        m = mastery.get(t.id)
        return m is not None and m.due_at is not None and m.due_at <= now

    def weakness(t):  # practised and weak first, then untried, then the rest
        m = level(t)
        return (0, m) if m is not None and m < GOOD_SCORE else (1, 0) if m is None else (2, m)

    weakest = sorted(current, key=weakness)
    due_now = sorted((t for t in current if due(t)), key=lambda t: mastery[t.id].due_at)
    due_now += [t for t in weakest if t not in due_now]
    review = sorted((t for t in earlier if due(t) or (level(t) is not None and level(t) < GOOD_SCORE)),
                    key=lambda t: (not due(t), level(t)))

    def at(topics, i):
        return topics[i % len(topics)]

    R, U, A = BloomLevel.REMEMBER, BloomLevel.UNDERSTAND, BloomLevel.APPLY
    slots = [
        Slot(at(recent, 0), "mcq", R), Slot(at(recent, 1), "mcq", U), Slot(at(due_now, 0), "mcq", U),
        Slot(at(weakest, 0), "mcq", R), Slot(at(recent, 2), "voice", U), Slot(at(due_now, 1), "mcq", A),
        Slot(review[0], "mcq", A, review=True) if review else Slot(at(weakest, 2), "mcq", A),
        Slot(at(weakest, 1), "voice", U),
    ]
    strong = next((s for s in slots if s.kind == "mcq" and (level(s.topic) or 0) >= HIGH_MASTERY), None)
    if strong is not None:
        strong.bloom_level = BloomLevel.ANALYZE
    return slots


# ----------------------------------------------------------- question pool
async def _seen(session: AsyncSession, student_id: uuid.UUID, today: date) -> set[uuid.UUID]:
    """Pool questions the student got in the last 14 days."""
    return set((await session.execute(
        select(DailyPracticeItem.practice_question_id)
        .join(DailyPracticeSession, DailyPracticeSession.id == DailyPracticeItem.session_id)
        .where(DailyPracticeSession.student_id == student_id,
               DailyPracticeSession.practice_date >= today - timedelta(days=NO_REPEAT_DAYS)))).scalars().all())


def from_enabled_bank():
    """A pool question made from a bank question counts only while its dataset is
    switched on somewhere - switching a dataset off retires what came from it.
    ponytail: the pool is shared, so "on for any school"; per-school if schools
    ever differ in which banks they use."""
    return or_(PracticeQuestion.source_question_id.is_(None), PracticeQuestion.source_question_id.in_(
        select(DatasetQuestion.id).join(SchoolDataset, SchoolDataset.dataset_id == DatasetQuestion.dataset_id)
        .where(SchoolDataset.enabled.is_(True))))


async def _from_pool(session: AsyncSession, topic: Topic, kind: str, level: BloomLevel,
                     exclude: set[uuid.UUID], exact: bool = False) -> PracticeQuestion | None:
    """The least used pool question on the topic - the slot's Bloom level first
    (only that level when `exact`), then real question-bank ones before written ones."""
    query = select(PracticeQuestion).where(PracticeQuestion.topic_id == topic.id, PracticeQuestion.kind == kind,
                                           PracticeQuestion.id.not_in(list(exclude)), from_enabled_bank())
    if exact:
        query = query.where(PracticeQuestion.bloom_level == level)
    return (await session.execute(
        query.order_by((PracticeQuestion.bloom_level == level).desc(), (PracticeQuestion.origin == "bank").desc(),
                       PracticeQuestion.used_count,
                  PracticeQuestion.created_at, PracticeQuestion.id).limit(1))).scalar_one_or_none()


async def _import_bank(session: AsyncSession, dataset_ids: list[uuid.UUID], topic: Topic, kind: str) -> int:
    """Moves up to BANK_IMPORT_LIMIT of the topic's bank questions not yet in
    the pool into it: MCQs with 4 usable options, or short answers speakable
    as a recorded answer (expected_points are filled by practice_voice)."""
    if not dataset_ids:
        return 0
    query = select(DatasetQuestion).where(
        DatasetQuestion.dataset_id.in_(dataset_ids), DatasetQuestion.topic_id == topic.id,
        text_only_filter(pictures=True),  # the screen draws the picture above either kind
        ~exists().where(PracticeQuestion.source_question_id == DatasetQuestion.id))
    if kind == "mcq":
        query = query.where(DatasetQuestion.question_type.in_((QuestionType.MCQ, QuestionType.TRUE_FALSE)),
                            DatasetQuestion.options.is_not(None))
    else:
        query = query.where(DatasetQuestion.question_type == QuestionType.SHORT_ANSWER,
                            func.length(DatasetQuestion.text) <= VOICE_MAX_CHARS)
    # a topic's spoken candidates are read in full: the unspeakable ones are skipped, so a capped read
    # could be all of them, every time
    rows = (await session.execute(query.order_by(DatasetQuestion.id)
                                  .limit(BANK_IMPORT_LIMIT * 3 if kind == "mcq" else None))).scalars().all()
    added, uids = [], set()
    for q in rows:
        uid = q.external_uid or str(q.id)  # the same question sits in several datasets
        index = correct_index(q.options, q.answer) if kind == "mcq" else None
        if (uid in uids or (kind == "mcq" and index is None)
                or (kind == "voice" and speakable.problem(q.text, q.question_type))):
            continue
        uids.add(uid)
        added.append(PracticeQuestion(
            chapter_id=topic.chapter_id, topic_id=topic.id, bloom_level=q.bloom_level, kind=kind,
            text=strip_inline_options(q.text, q.options) if kind == "mcq" else q.text,
            options=[unlabel(o) for o in q.options] if kind == "mcq" else None, correct_index=index,
            expected_points=[], misconceptions=[], source_question_id=q.id, origin="bank", used_count=0))
        if len(added) == BANK_IMPORT_LIMIT:
            break
    session.add_all(added)
    await session.flush()
    return len(added)


async def _generate(session: AsyncSession, school_class: SchoolClass, chapter_id: uuid.UUID,
                    slots: list[Slot], seed: str) -> dict[int, PracticeQuestion]:
    """MCQs written by the generation service for slots nothing else could
    fill, by position in `slots`, stored in the pool. Best effort."""
    if not slots or not generation_client.is_configured():
        return {}
    chapter = await session.get(Chapter, chapter_id)
    sections = []
    for topic_id in dict.fromkeys(s.topic.id for s in slots):
        sections += (await _sections(session, SimpleNamespace(chapter_id=chapter_id), topic_id))[1]
    if not sections:
        return {}
    subject = await session.get(Subject, chapter.subject_id)
    payload = {
        "subject": subject.name if subject else "Science",
        "grade": school_class.grade,
        "chapter": chapter.name,
        "sections": sections,
        "slots": [{"bloom_level": s.bloom_level.value, "difficulty": "MEDIUM", "question_type": "mcq",
                   "concept": s.topic.name, "misconceptions": []} for s in slots],
        "examples": [],
        "avoid": [],
    }
    try:
        with ai_context.feature("daily_practice_mcq"):
            result = await generation_client.generate_mcq(payload)
    except generation_client.GenerationServiceUnavailableError:
        logger.warning("daily_practice_generation_unavailable", extra={"chapter_id": str(chapter_id)})
        return {}
    made = {}
    for g in result.get("questions", []):
        index, slot_index = correct_index(g.get("options"), g.get("answer", "")), g.get("slot_index", -1)
        if index is None or not 0 <= slot_index < len(slots) or slot_index in made:
            continue
        slot = slots[slot_index]
        options, index, explanation = shuffle_options([str(o).strip() for o in g["options"]], index,
                                                      g.get("explanation") or "", f"{seed}:{slot_index}")
        made[slot_index] = PracticeQuestion(
            chapter_id=chapter_id, topic_id=slot.topic.id, bloom_level=slot.bloom_level, kind="mcq",
            text=strip_inline_options(g["text"], options), options=options, correct_index=index,
            explanation=explanation or None, hint=g.get("hint") or None,
            expected_points=[], misconceptions=[], origin="generated", used_count=0)
    session.add_all(made.values())
    await session.flush()
    return made


async def _generate_special(session: AsyncSession, school_class: SchoolClass, slots: list[Slot],
                            indexes: list[int]) -> dict[int, PracticeQuestion]:
    """who_is_right / make_your_own / fix_steps questions written by the
    generation service for these slots (by index), kept in the pool only if
    their spec holds together (question_types.validate - a make_your_own's
    own example must pass its check). Best effort: the rest become MCQs."""
    if not indexes or not generation_client.is_configured():
        return {}
    made: dict[int, PracticeQuestion] = {}
    for topic_id in dict.fromkeys(slots[i].topic.id for i in indexes):
        mine = [i for i in indexes if slots[i].topic.id == topic_id]
        topic = slots[mine[0]].topic
        chapter = await session.get(Chapter, topic.chapter_id)
        subject = await session.get(Subject, chapter.subject_id) if chapter else None
        _, sections = await _sections(session, SimpleNamespace(chapter_id=topic.chapter_id), topic.id)
        if not sections:
            continue
        avoid = (await session.execute(select(PracticeQuestion.text).where(
            PracticeQuestion.topic_id == topic.id, PracticeQuestion.kind.in_(question_types.KINDS))
            .order_by(PracticeQuestion.created_at.desc()).limit(20))).scalars().all()
        payload = {
            "grade": school_class.grade, "subject": subject.name if subject else "Science",
            "chapter": chapter.name if chapter else "", "topic": tidy(topic.name),
            "context": "\n\n".join(f"{s['title']}\n{s['text']}" for s in sections),
            "slots": [{"kind": slots[i].kind, "bloom_level": slots[i].bloom_level.value} for i in mine],
            "checks": [{"kind": k, "describe": c.describe} for k, c in answer_checks.KINDS.items()],
            "avoid": list(avoid),
        }
        try:
            with ai_context.feature("special_questions"):
                body = await generation_client.special_questions(payload)
        except generation_client.GenerationServiceUnavailableError:
            logger.warning("special_questions_unavailable", extra={"topic_id": str(topic.id)})
            continue
        for g in body.get("questions", []):
            n = g.get("slot_index", -1)
            if not 0 <= n < len(mine) or mine[n] in made or g.get("kind") != slots[mine[n]].kind:
                continue
            try:
                question_types.validate(g["kind"], g.get("spec") or {})
            except question_types.SpecError as exc:
                logger.warning("special_question_rejected", extra={"kind": g["kind"], "reason": str(exc)})
                continue
            slot = slots[mine[n]]
            made[mine[n]] = PracticeQuestion(
                chapter_id=topic.chapter_id, topic_id=topic.id, bloom_level=slot.bloom_level, kind=slot.kind,
                text=g["text"], spec=spec, options=None, correct_index=None,
                explanation=spec.get("explanation") or None, expected_points=[], misconceptions=[],
                origin="generated", used_count=0)
    session.add_all(made.values())
    await session.flush()
    return made


async def _explain(questions: list[PracticeQuestion], topic_names: dict) -> None:
    """One batched generation call writes the "why" and hint of every MCQ
    lacking them; kept on the pool row so it's written once. Best effort -
    explanation_of/hint_of cover the rest."""
    todo = [q for q in questions if q.kind == "mcq" and (not q.explanation or not q.hint)]
    if not todo or not generation_client.is_configured():
        return
    items = [{"question_number": n, "text": q.text, "options": q.options, "correct_index": q.correct_index,
              "concept": topic_names.get(q.topic_id, "")} for n, q in enumerate(todo, start=1)]
    try:
        with ai_context.feature("daily_practice_mcq"):
            body = await generation_client.explain_mcq({"items": items})
    except generation_client.GenerationServiceUnavailableError:
        logger.warning("daily_practice_explain_unavailable", extra={"count": len(items)})
        return
    for r in body.get("results") or []:
        n = int(r.get("question_number") or 0)
        if 1 <= n <= len(todo):
            q = todo[n - 1]
            q.explanation = q.explanation or r.get("explanation") or None
            q.hint = q.hint or r.get("hint") or None


async def _prepare_voice(session: AsyncSession, questions: list[PracticeQuestion]) -> None:
    todo = [q for q in questions if q.kind == "voice" and not q.expected_points]
    if not todo:
        return
    from src.services.practice_voice import prepare_voice_questions  # lazy: practice_voice imports this module

    await prepare_voice_questions(session, todo)


def same_question(text: str) -> str:
    """A key that's equal for the same question worded with other spacing or punctuation."""
    return re.sub(r"\W+", "", text.lower())[:80]


_STOP = frozenset("what which that this with from when does about following these those their there into have been "
                  "will would should could because while where than then them they your".split())


def words_of(text: str) -> frozenset[str]:
    return frozenset(w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 3 and w not in _STOP)


def near_duplicate(text: str, seen: list[frozenset[str]]) -> bool:
    """True when most of the stem's content words are in one already picked (C-012: three differently
    worded "what does silver chloride do in sunlight" questions in one quiz). ponytail: word overlap,
    not meaning - two questions on one idea with no shared words still pass."""
    mine = words_of(text)
    return any(len(mine & other) >= 4 and len(mine & other) >= 0.7 * min(len(mine), len(other)) for other in seen)


async def _fill(session: AsyncSession, student: User, school_class: SchoolClass, slots: list[Slot],
                today: date, seed: str, exact_level: bool = False,
                write_special: bool = True) -> list[tuple[Slot, PracticeQuestion]]:
    """A question per slot: pool, then bank import, then generation. A voice
    slot with no speakable question on its topic becomes an MCQ; a slot
    nothing can fill is dropped. `exact_level` (topic quizzes, which score
    per Bloom level): pool and bank only at the slot's own level, generation
    for the rest, and only then a question of another level. Without
    `write_special` a who_is_right/... slot takes one already written or an
    MCQ - never a 40 s model call while the student waits."""
    exclude = await _seen(session, student.id, today)
    dataset_ids = [d.id for d in await visible_datasets(session, student) if d.type == DatasetType.QA]
    picked: dict[int, PracticeQuestion] = {}
    # real question-bank (board paper) questions first: bring a batch of each topic's into the pool
    # before choosing, so they're picked ahead of written ones (_from_pool prefers origin "bank")
    for topic in dict.fromkeys(slot.topic for slot in slots):
        for kind in ("mcq", "voice"):
            await _import_bank(session, dataset_ids, topic, kind)
    seen_texts: set[str] = set()
    seen_words: list[frozenset[str]] = []

    async def pool(topic: Topic, kind: str, level: BloomLevel, exact: bool) -> PracticeQuestion | None:
        # the same question twice (one board question in two years' papers) only once
        for _ in range(10):
            q = await _from_pool(session, topic, kind, level, exclude, exact)
            if q is None or (same_question(q.text) not in seen_texts and not near_duplicate(q.text, seen_words)):
                return q
            exclude.add(q.id)
        return None

    topics = list(dict.fromkeys(slot.topic for slot in slots))
    for i, slot in enumerate(slots):
        # a who_is_right/make_your_own/fix_steps slot waits for generation before it settles for an MCQ
        for kind in ([slot.kind] if slot.kind in question_types.KINDS and write_special
                     else dict.fromkeys([slot.kind, "mcq"])):
            exact = exact_level and kind == "mcq"  # spoken questions are few: any level, scored at its own
            q = await pool(slot.topic, kind, slot.bloom_level, exact)
            if q is None and kind in ("mcq", "voice") and await _import_bank(session, dataset_ids, slot.topic, kind):
                q = await pool(slot.topic, kind, slot.bloom_level, exact)
            if q is None and kind in question_types.KINDS:
                # none of this type on the topic (an introduction rarely has a worked problem): a sibling
                # topic's keeps the type the teacher asked for
                for other in topics:
                    if other is not slot.topic and (q := await pool(other, kind, slot.bloom_level, exact)):
                        slot = replace(slot, topic=other)
                        break
            if q is not None:
                seen_texts.add(same_question(q.text))
                seen_words.append(words_of(q.text))
                slots[i] = replace(slot, kind=kind)
                picked[i] = q
                exclude.add(q.id)
                break
    special = [i for i in range(len(slots))
               if write_special and i not in picked and slots[i].kind in question_types.KINDS]
    for i, q in (await _generate_special(session, school_class, slots, special)).items():
        picked[i] = q
        exclude.add(q.id)
    open_slots = [i for i in range(len(slots)) if i not in picked]
    for chapter_id in dict.fromkeys(slots[i].topic.chapter_id for i in open_slots):
        mine = [i for i in open_slots if slots[i].topic.chapter_id == chapter_id]
        for n, q in (await _generate(session, school_class, chapter_id,
                                     [replace(slots[i], kind="mcq") for i in mine], seed)).items():
            slots[mine[n]] = replace(slots[mine[n]], kind="mcq")
            picked[mine[n]] = q
            exclude.add(q.id)
    if exact_level:  # generation couldn't help: any level beats no question
        for i in [i for i in range(len(slots)) if i not in picked]:
            q = await _from_pool(session, slots[i].topic, "mcq", slots[i].bloom_level, exclude)
            if q is not None:
                slots[i] = replace(slots[i], kind="mcq")
                picked[i] = q
                exclude.add(q.id)
    await _prepare_voice(session, list(picked.values()))
    # never a spoken item without its spoken wording (C-002): an MCQ on the topic instead, else none
    for i in [i for i, q in picked.items() if q.kind == "voice" and not q.expected_points]:
        del picked[i]
        q = await pool(slots[i].topic, "mcq", slots[i].bloom_level, False)
        if q is not None:
            slots[i], picked[i] = replace(slots[i], kind="mcq"), q
            exclude.add(q.id)
    chosen = [(slots[i], picked[i]) for i in sorted(picked)]
    questions = [q for _, q in chosen]
    await _explain(questions, {s.topic.id: s.topic.name for s, _ in chosen})
    for q in questions:
        q.used_count = (q.used_count or 0) + 1
    logger.info("daily_practice_built", extra={"student_id": str(student.id), "items": len(chosen),
                                               "generated": sum(q.origin == "generated" for q in questions)})
    return chosen


# ------------------------------------------------------------------ session
def extension_of(order: int) -> int:
    """0 for the day's first set, n for the n-th extension's items."""
    return order // EXTENSION_ORDER


def latest_extension(items: list[tuple[DailyPracticeItem, PracticeQuestion, str | None]]) -> tuple[int, list[str]]:
    """(unanswered items of the latest extension, their topic names) - (0, []) if none."""
    band = max((extension_of(item.order) for item, _, _ in items), default=0)
    fresh = [(item, name) for item, _, name in items
             if band and extension_of(item.order) == band and item.answered_at is None]
    return len(fresh), list(dict.fromkeys(tidy(name) for _, name in fresh if name))


def tidy(name: str) -> str:
    """Title Case for an ALL CAPS textbook heading (as the app's tidyTitle); others unchanged."""
    if not name.isupper():
        return name
    words = name.lower().split(" ")
    return " ".join(w if n and w in SMALL_WORDS else w[:1].upper() + w[1:] for n, w in enumerate(words))


async def _newly_taught(session: AsyncSession, practice: DailyPracticeSession,
                        taught: list[tuple[ClassTopicProgress, Topic]]) -> tuple[list[Topic], list[DailyPracticeItem]]:
    """Topics marked taught after the session was built that none of its
    items is on yet (most recently taught first), and the session's items."""
    items = list((await session.execute(select(DailyPracticeItem).where(
        DailyPracticeItem.session_id == practice.id).order_by(DailyPracticeItem.order))).scalars().all())
    have = {i.topic_id for i in items}
    return [t for p, t in taught if p.created_at > practice.created_at and t.id not in have], items


async def _extend(session: AsyncSession, student: User, school_class: SchoolClass, practice: DailyPracticeSession,
                  taught: list[tuple[ClassTopicProgress, Topic]], today: date) -> None:
    """Appends up to 4 MCQs on topics taught since the session was built (at
    most 2 extensions, 16 items a day) and reopens a completed session -
    completed_at stays, so complete() won't pay the bonus twice. The row
    lock makes a concurrent open wait and then find the topics covered."""
    await session.execute(select(DailyPracticeSession).where(DailyPracticeSession.id == practice.id)
                          .with_for_update().execution_options(populate_existing=True))
    new, items = await _newly_taught(session, practice, taught)
    band = max((extension_of(i.order) for i in items), default=0)
    room = min(EXTENSION_ITEMS, MAX_ITEMS - len(items))
    if not new or band >= MAX_EXTENSIONS or room <= 0:
        return
    levels = [BloomLevel.REMEMBER, BloomLevel.UNDERSTAND, BloomLevel.APPLY]
    slots = [Slot(new[n % len(new)], "mcq", levels[n // len(new) % 3]) for n in range(room)]
    # ponytail: a new topic with no question anywhere is retried on every open; store a marker if that bites
    chosen = await _fill(session, student, school_class, slots, today, f"{practice.id}:{band + 1}")
    if not chosen:
        return
    start = (band + 1) * EXTENSION_ORDER
    session.add_all([DailyPracticeItem(session_id=practice.id, order=start + n, practice_question_id=q.id,
                                       kind=q.kind, topic_id=slot.topic.id, bloom_level=q.bloom_level, review=False)
                     for n, (slot, q) in enumerate(chosen, start=1)])
    practice.status = "open"
    await session.flush()
    logger.info("daily_practice_extended", extra={"student_id": str(student.id), "items": len(chosen)})


async def spoken_allowed(session: AsyncSession, student: User, today: date) -> bool:
    """Whether today's practice may include spoken answers: the plan has them,
    and the student has had them on fewer than its spoken_days_per_week days
    this week (Monday on; NULL = every day)."""
    found = await get_active_plan(session, student.school_id) if student.school_id else None
    if not is_feature_enabled(Feature.SPOKEN_ANSWERS, plan=found):
        return False
    if found is None or found.spoken_days_per_week is None:
        return True
    days = (await session.execute(
        select(func.count(func.distinct(DailyPracticeSession.practice_date)))
        .join(DailyPracticeItem, DailyPracticeItem.session_id == DailyPracticeSession.id)
        .where(DailyPracticeSession.student_id == student.id, DailyPracticeItem.kind == "voice",
               DailyPracticeSession.practice_date >= today - timedelta(days=today.weekday()),
               DailyPracticeSession.practice_date < today))).scalar_one()
    return days < found.spoken_days_per_week


async def ensure_today(session: AsyncSession, student: User) -> tuple[DailyPracticeSession | None, str | None]:
    """Today's session, built on first call; or (None, empty reason). The
    insert is ON CONFLICT DO NOTHING on (student, date): a concurrent open
    waits for the first one's commit and then reads its session.
    Topics taught after it was built: an unstarted session is rebuilt, a
    started (or completed) one is extended (_extend). Caller commits."""
    school_class = await class_of(session, student.id)
    taught = await taught_topics(session, school_class.id) if school_class else []
    if not taught:
        return None, "no_topics_taught"
    today = today_ist()
    practice = (await session.execute(select(DailyPracticeSession).where(
        DailyPracticeSession.student_id == student.id, DailyPracticeSession.practice_date == today,
        DailyPracticeSession.topic_id.is_(None), DailyPracticeSession.quiz_test_id.is_(None)))).scalar_one_or_none()
    if practice is not None:
        new, items = await _newly_taught(session, practice, taught)
        if not new:
            return practice, None
        if any(i.answered_at is not None for i in items):
            await _extend(session, student, school_class, practice, taught, today)
            return practice, None
        # nothing answered yet: rebuild with the current plan. A concurrent rebuild deletes nothing
        # and then waits on the insert below, like a concurrent first open.
        await session.execute(delete(DailyPracticeSession).where(
            DailyPracticeSession.id == practice.id,
            ~exists().where(DailyPracticeItem.session_id == practice.id, DailyPracticeItem.answered_at.is_not(None))))
        session.expunge(practice)
    created = (await session.execute(pg_insert(DailyPracticeSession).values(
        id=uuid.uuid4(), student_id=student.id, practice_date=today, chapter_id=taught[0][0].chapter_id,
        status="open", xp_awarded=0,
    ).on_conflict_do_nothing(index_elements=["student_id", "practice_date"],
                             index_where=DailyPracticeSession.topic_id.is_(None)
                             & DailyPracticeSession.quiz_test_id.is_(None))
        .returning(DailyPracticeSession.id))).scalar_one_or_none()
    practice = (await session.execute(select(DailyPracticeSession).where(
        DailyPracticeSession.student_id == student.id, DailyPracticeSession.practice_date == today,
        DailyPracticeSession.topic_id.is_(None), DailyPracticeSession.quiz_test_id.is_(None))
        .execution_options(populate_existing=True))).scalar_one()
    if created is None:
        return practice, None

    mastery = {m.topic_id: m for m in (await session.execute(select(StudentTopicMastery).where(
        StudentTopicMastery.student_id == student.id))).scalars().all()}
    # ponytail: holds the session row and a connection through the LLM calls on a cold pool (~1 min)
    slots = plan(taught, mastery, datetime.now(timezone.utc))
    if not await spoken_allowed(session, student, today):
        slots = [replace(s, kind="mcq") if s.kind == "voice" else s for s in slots]
    chosen = await _fill(session, student, school_class, slots, today, str(practice.id))
    if not chosen:
        await session.delete(practice)
        await session.flush()
        return None, "no_questions"
    session.add_all([DailyPracticeItem(session_id=practice.id, order=order, practice_question_id=q.id, kind=q.kind,
                                       topic_id=slot.topic.id, bloom_level=q.bloom_level, review=slot.review)
                     for order, (slot, q) in enumerate(chosen, start=1)])
    await session.flush()
    return practice, None


async def items_of(session: AsyncSession,
                   session_id: uuid.UUID) -> list[tuple[DailyPracticeItem, PracticeQuestion, str | None]]:
    """(item, question, topic name) in order."""
    return [tuple(r) for r in (await session.execute(
        select(DailyPracticeItem, PracticeQuestion, Topic.name)
        .join(PracticeQuestion, PracticeQuestion.id == DailyPracticeItem.practice_question_id)
        .outerjoin(Topic, Topic.id == DailyPracticeItem.topic_id)
        .where(DailyPracticeItem.session_id == session_id).order_by(DailyPracticeItem.order))).all()]


async def own_session(session: AsyncSession, session_id: str, student: User) -> DailyPracticeSession:
    try:
        sid = uuid.UUID(session_id)
    except ValueError:
        raise NotFoundError("Daily practice not found.") from None
    practice = (await session.execute(select(DailyPracticeSession).where(
        DailyPracticeSession.id == sid, DailyPracticeSession.student_id == student.id))).scalar_one_or_none()
    if practice is None:
        raise NotFoundError("Daily practice not found.")
    return practice


async def load_item_for_student(session: AsyncSession, session_id: str, item_id: str,
                                student: User) -> tuple[DailyPracticeSession, DailyPracticeItem, PracticeQuestion]:
    """The student's own item - 404 otherwise, 409 once the session is completed."""
    practice = await own_session(session, session_id, student)
    try:
        iid = uuid.UUID(item_id)
    except ValueError:
        raise NotFoundError("Practice item not found.") from None
    item = (await session.execute(select(DailyPracticeItem).where(
        DailyPracticeItem.id == iid, DailyPracticeItem.session_id == practice.id))).scalar_one_or_none()
    if item is None:
        raise NotFoundError("Practice item not found.")
    if practice.status == "completed":
        raise ConflictError("Today's practice is already completed.")
    return practice, item, await session.get(PracticeQuestion, item.practice_question_id)


# ------------------------------------------------------------------ answers
def next_mastery(old: int | None, score: int) -> int:
    return score if old is None else round(0.7 * old + 0.3 * score)


def replay(scores: list[int]) -> int | None:
    """Mastery after these scores in order (None before any)."""
    mastery = None
    for score in scores:
        mastery = next_mastery(mastery, score)
    return mastery


def interval_days(score: int, good_streak: int) -> int:
    """Days until the topic is due again; good_streak = good answers in a row before this one."""
    return INTERVAL_DAYS[min(good_streak, len(INTERVAL_DAYS) - 1)] if score >= GOOD_SCORE else 1


def voice_xp(score: int, transcript: str | None) -> int:
    if score >= GOOD_SCORE:
        return 10
    if score >= 40:
        return 5
    return 2 if (transcript or "").strip() else 0


async def combo_of(session: AsyncSession, item: DailyPracticeItem) -> int:
    """Correct MCQs in a row (item order, recorded answers skipped) ending at this item."""
    run = 0
    for i in (await session.execute(select(DailyPracticeItem).where(
            DailyPracticeItem.session_id == item.session_id, DailyPracticeItem.kind == "mcq")
            .order_by(DailyPracticeItem.order))).scalars().all():
        run = run + 1 if i.correct else 0
        if i.id == item.id:
            return run
    return 0


async def _topic_scores(session: AsyncSession, student_id, topic_id, *, before: date | None = None,
                        exclude_item=None) -> list[int]:
    """The student's scores on a topic, oldest first."""
    query = (select(DailyPracticeItem.score)
             .join(DailyPracticeSession, DailyPracticeSession.id == DailyPracticeItem.session_id)
             .where(DailyPracticeSession.student_id == student_id, DailyPracticeItem.topic_id == topic_id,
                    DailyPracticeItem.answered_at.is_not(None), DailyPracticeItem.score.is_not(None))
             .order_by(DailyPracticeItem.answered_at))
    if before is not None:
        query = query.where(DailyPracticeSession.practice_date < before)
    if exclude_item is not None:
        query = query.where(DailyPracticeItem.id != exclude_item)
    return list((await session.execute(query)).scalars().all())


async def _update_mastery(session: AsyncSession, student_id, item: DailyPracticeItem, score: int) -> None:
    topic = await session.get(Topic, item.topic_id)
    await session.execute(pg_insert(StudentTopicMastery).values(
        id=uuid.uuid4(), student_id=student_id, topic_id=topic.id, chapter_id=topic.chapter_id,
        mastery=0, attempts=0, correct=0,
    ).on_conflict_do_nothing(index_elements=["student_id", "topic_id"]))
    row = (await session.execute(select(StudentTopicMastery).where(
        StudentTopicMastery.student_id == student_id, StudentTopicMastery.topic_id == topic.id)
        .with_for_update().execution_options(populate_existing=True))).scalar_one()
    streak = 0
    for earlier in reversed(await _topic_scores(session, student_id, topic.id, exclude_item=item.id)):
        if earlier < GOOD_SCORE:
            break
        streak += 1
    now = datetime.now(timezone.utc)
    row.mastery = next_mastery(row.mastery if row.attempts else None, score)
    row.attempts += 1
    row.correct += score >= GOOD_SCORE
    row.last_practised_at = now
    row.due_at = now + timedelta(days=interval_days(score, streak))


async def record_item_result(session: AsyncSession, item: DailyPracticeItem, *, score: int,
                             correct: bool | None = None) -> int:
    """Marks an item just answered (the caller has set its answer and
    answered_at - and transcript for a recorded answer): stores score and
    correct, moves the topic's mastery and awards the item's XP. Returns
    the XP. Caller commits."""
    item.score = score = max(0, min(100, int(score)))
    item.correct = score >= GOOD_SCORE if correct is None else correct
    practice = await session.get(DailyPracticeSession, item.session_id)
    if item.topic_id is not None:
        await _update_mastery(session, practice.student_id, item, score)
    if item.kind == "mcq":
        xp = MCQ_XP + (COMBO_BONUS if await combo_of(session, item) >= COMBO_FROM else 0) if item.correct else 0
    else:
        xp = voice_xp(score, item.transcript)
    item.xp_awarded = xp
    practice.xp_awarded = (practice.xp_awarded or 0) + xp
    await award_points(session, practice.student_id, xp)
    await apply_xp(session, practice.student_id, xp)
    return xp


async def answer_mcq(session: AsyncSession, item: DailyPracticeItem, question: PracticeQuestion,
                     option_index: int, elapsed_ms: int | None) -> dict:
    """Marks the first answer; a repeat returns the stored result. Caller commits."""
    if item.kind != "mcq":
        raise ValidationError("This question takes a recorded answer.")
    if not 0 <= option_index < len(question.options or []):
        raise ValidationError("No such option.")
    # lock the item so two taps can't both be the first answer
    await session.execute(select(DailyPracticeItem).where(DailyPracticeItem.id == item.id).with_for_update()
                          .execution_options(populate_existing=True))
    if item.answered_at is None:
        right = option_index == question.correct_index
        item.option_index, item.elapsed_ms, item.answered_at = option_index, elapsed_ms, datetime.now(timezone.utc)
        await record_item_result(session, item, score=100 if right else 0, correct=right)
    return {"correct": bool(item.correct), "correct_index": question.correct_index,
            "explanation": explanation(question), "xp_awarded": item.xp_awarded,
            "combo": await combo_of(session, item)}


async def answer_special(session: AsyncSession, item: DailyPracticeItem, question: PracticeQuestion,
                         response: dict) -> None:
    """Marks the first answer to a who_is_right / make_your_own / fix_steps
    item (question_types.grade); a repeat keeps the stored one. Caller commits."""
    if item.kind not in question_types.KINDS:
        raise ValidationError("This question is answered another way.")
    await session.execute(select(DailyPracticeItem).where(DailyPracticeItem.id == item.id).with_for_update()
                          .execution_options(populate_existing=True))
    if item.answered_at is not None:
        return
    graded = question_types.grade(item.kind, question.spec or {}, response)
    item.response = response
    item.feedback = {"message": graded["message"], "mistake": graded["mistake"]}
    item.answered_at = datetime.now(timezone.utc)
    await record_item_result(session, item, score=graded["score"], correct=graded["correct"])


def explanation(question: PracticeQuestion) -> str:
    return explanation_of(question.options, question.correct_index, question.explanation)


# --------------------------------------------------------------- completion
def streak_days(dates: set[date], today: date) -> int:
    """Consecutive practised days ending today, or yesterday if not yet today."""
    day = today if today in dates else today - timedelta(days=1)
    n = 0
    while day in dates:
        n += 1
        day -= timedelta(days=1)
    return n


async def completed_dates(session: AsyncSession, student_ids, upto: date) -> dict[uuid.UUID, set[date]]:
    # ponytail: every completed date ever, fine for a school year of rows; bound it if streaks get long
    out: dict[uuid.UUID, set[date]] = {s: set() for s in student_ids}
    for student_id, day in (await session.execute(
            select(DailyPracticeSession.student_id, DailyPracticeSession.practice_date).where(
                DailyPracticeSession.student_id.in_(list(out)),
                # an extension reopens a completed day; it still counts
                or_(DailyPracticeSession.status == "completed", DailyPracticeSession.completed_at.is_not(None)),
                DailyPracticeSession.practice_date <= upto))).all():
        out[student_id].add(day)
    return out


async def student_streak(session: AsyncSession, student_id: uuid.UUID) -> int:
    today = today_ist()
    return streak_days((await completed_dates(session, [student_id], today))[student_id], today)


async def complete(session: AsyncSession, practice: DailyPracticeSession, skippable_voice: bool = False) -> dict:
    """Closes the session once every item is answered (409 before), adding the
    completion bonus once (not again after an extension reopened it); calling
    again returns the same summary. Mastery
    before/after per topic practised: before = from every earlier day's
    answers, after = including this day's. Caller commits."""
    items = await items_of(session, practice.id)
    if any(item.answered_at is None and not (skippable_voice and item.kind == "voice") for item, _, _ in items):
        raise ConflictError("Answer every question first.")
    claimed = (await session.execute(
        update(DailyPracticeSession).where(DailyPracticeSession.id == practice.id,
                                           DailyPracticeSession.status == "open",
                                           DailyPracticeSession.completed_at.is_(None))
        .values(status="completed", completed_at=datetime.now(timezone.utc))
        .returning(DailyPracticeSession.completed_at))).scalar_one_or_none()
    if claimed is None and practice.status == "open":  # finished an extension: no second bonus
        await session.execute(update(DailyPracticeSession).where(DailyPracticeSession.id == practice.id)
                              .values(status="completed"))
        practice.status = "completed"
    if claimed is not None:
        practice.status, practice.completed_at = "completed", claimed
        practice.xp_awarded = (practice.xp_awarded or 0) + COMPLETION_XP
        await award_points(session, practice.student_id, COMPLETION_XP)
        await apply_xp(session, practice.student_id, COMPLETION_XP)
        await session.flush()
    mastery = []
    for topic_id, name in dict.fromkeys((item.topic_id, name) for item, _, name in items if item.topic_id):
        before = replay(await _topic_scores(session, practice.student_id, topic_id, before=practice.practice_date))
        after = replay(await _topic_scores(session, practice.student_id, topic_id,
                                           before=practice.practice_date + timedelta(days=1)))
        mastery.append({"topic_name": name, "before": before or 0, "after": after or 0})
    return {"xp_awarded": COMPLETION_XP, "streak_days": await student_streak(session, practice.student_id),
            "mastery": mastery}


# ------------------------------------------------------------------ teacher
async def class_summary(session: AsyncSession, class_id: uuid.UUID, day: date) -> dict:
    """Who practised on `day`, streaks, the current chapter's topics most
    students struggle with, and who is slipping (no practice for 3+ days
    while classmates practise, or a topic's mastery down 15+ points this week)."""
    students = (await session.execute(
        select(User.id, User.display_name).join(StudentProfile, StudentProfile.user_id == User.id)
        .where(StudentProfile.class_id == class_id).order_by(User.display_name))).all()
    ids = [s for s, _ in students]
    dates = await completed_dates(session, ids, day)
    window = {day - timedelta(days=n) for n in range(IDLE_DAYS)}
    active = {s for s in ids if dates[s] & window}

    chapter = await current_chapter(session, class_id)
    weakest = []
    if chapter is not None and ids:
        taught = [t for p, t in await taught_topics(session, class_id) if p.chapter_id == chapter.id]
        rows = (await session.execute(select(StudentTopicMastery.topic_id, StudentTopicMastery.mastery).where(
            StudentTopicMastery.student_id.in_(ids), StudentTopicMastery.topic_id.in_([t.id for t in taught]),
            StudentTopicMastery.attempts > 0))).all()
        for t in taught:
            levels = [m for topic_id, m in rows if topic_id == t.id]
            struggling = round(100 * sum(m < STRUGGLING_BELOW for m in levels) / len(levels)) if levels else 0
            if struggling:
                weakest.append({"topic_id": str(t.id), "topic_name": t.name, "struggling_percent": struggling})
        weakest = sorted(weakest, key=lambda w: -w["struggling_percent"])[:3]

    week_start = day - timedelta(days=6)
    history: dict[tuple, list[tuple[date, int]]] = {}
    names: dict[uuid.UUID, str] = {}
    for student_id, topic_id, topic_name, practised_on, score in (await session.execute(
            select(DailyPracticeSession.student_id, Topic.id, Topic.name, DailyPracticeSession.practice_date,
                   DailyPracticeItem.score)
            .join(DailyPracticeSession, DailyPracticeSession.id == DailyPracticeItem.session_id)
            .join(Topic, Topic.id == DailyPracticeItem.topic_id)
            .where(DailyPracticeSession.student_id.in_(ids), DailyPracticeSession.practice_date <= day,
                   DailyPracticeItem.score.is_not(None), DailyPracticeItem.answered_at.is_not(None))
            .order_by(DailyPracticeItem.answered_at))).all() if ids else []:
        history.setdefault((student_id, topic_id), []).append((practised_on, score))
        names[topic_id] = topic_name

    slipping = []
    for student_id, name in students:
        reason = None
        if student_id not in active and active - {student_id}:
            reason = (f"No practice for {IDLE_DAYS}+ days" if dates[student_id]
                      else "Hasn't started daily practice")
        else:
            drops = []
            for (s, topic_id), scores in history.items():
                if s != student_id:
                    continue
                before = replay([sc for d, sc in scores if d < week_start])
                after = replay([sc for _, sc in scores])
                if before is not None and before - after >= DROP_POINTS:
                    drops.append((before - after, names[topic_id]))
            if drops:
                drop, topic_name = max(drops)
                reason = f"{topic_name} mastery dropped {drop} points this week"
        if reason:
            slipping.append({"student_id": str(student_id), "name": name, "reason": reason})

    # today's progress for students who started but haven't finished
    progress = {sid: (answered, total) for sid, answered, total in (await session.execute(
        select(DailyPracticeSession.student_id, func.count(DailyPracticeItem.answered_at), func.count(DailyPracticeItem.id))
        .join(DailyPracticeItem, DailyPracticeItem.session_id == DailyPracticeSession.id)
        .where(DailyPracticeSession.student_id.in_(ids), DailyPracticeSession.practice_date == day)
        .group_by(DailyPracticeSession.student_id))).all()} if ids else {}

    return {
        "date": day,
        "current_chapter": {"id": str(chapter.id), "name": chapter.name} if chapter else None,
        "student_count": len(students),
        "practised_count": sum(day in dates[s] for s in ids),
        "students": [{"id": str(s), "name": name, "practised_today": day in dates[s],
                      "answered_today": progress.get(s, (0, 0))[0], "items_today": progress.get(s, (0, 0))[1],
                      "streak_days": streak_days(dates[s], day),
                      "last_practised_on": max(dates[s], default=None)} for s, name in students],
        "weakest_topics": weakest,
        "slipping": slipping,
    }
