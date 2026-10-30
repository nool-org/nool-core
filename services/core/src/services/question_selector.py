"""Picks real question-bank questions (DatasetQuestion) for a homework or
paper - plain SQL + ranking, no LLM. Whatever it can't fill comes back as
`unfilled` slots for generation to cover.

Two steps:
- plan_slots(): turns "N questions, this Bloom mix, this difficulty" into N
  concrete (bloom, difficulty) slots.
- select_questions(): fills each slot from the given datasets, preferring
  the weak subtopic, then its topic, then the chapter; never the same
  question twice (the same question in two datasets shares external_uid);
  never one the caller says was already seen.

Access control is the caller's job: pass only dataset ids the user may use
(see repositories/dataset_access.py) intersected with the ones picked for
this homework.
"""
import random
import uuid
from dataclasses import dataclass, field

import re

from sqlalchemy import and_, false, func, literal_column, not_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.domain.models import BloomLevel, DatasetQuestion, HomeworkDifficulty, QuestionType
from src.domain.models.question_paper import PaperDifficultyLevel
from src.config.settings import get_settings
from src.services.assets import asset_url



def _figure(cond: str):
    return DatasetQuestion.figures.op("@?")(literal_column(f"'$[*] ? ({cond})'::jsonpath"))


_SHOWN = '!(@.role == "solution")'  # on the question or an option; a worked-solution figure blocks nothing


# A stem that points at a figure the bank import couldn't attach.
FIGURE_REF = r"\[image|(given|following|shown|adjacent) (figure|diagram|graph)|fig\."
# B23: what an LLM-written paper question must not say - the paper has no picture for it.
_LLM_FIGURE_REF = FIGURE_REF + r"|(in|from|see) the (figure|diagram|graph|circuit)|(figure|diagram|graph|circuit) (below|above)"


def references_missing_figure(text: str | None) -> bool:
    return bool(re.search(_LLM_FIGURE_REF, text or "", re.I))


# T-070: "as described in the text", "mentioned in the chapter" - the student only has the printed paper
UNSEEN_TEXT_REF = (r"(described|mentioned|discussed|stated|given|explained|shown|studied)"
                   r" (in|by) (the|this|your) (text|textbook|chapter|lesson|passage|section|unit|activity)")
_PARTS = re.compile(r"\(\s*(?:[a-e]|i{1,3}|iv)\s*\)|\?", re.I)


def asks_too_much_for_marks(text: str | None, marks: int) -> bool:
    """T-070: a 1-3 mark question with more parts than marks allow ("a 2-mark question with about 6 answers")."""
    parts = len(_PARTS.findall(text or ""))
    return marks <= 3 and parts > max(2, marks)


def has_unusable_options(options: list[str] | None) -> bool:
    """MCQ options of which two are the same question's same answer (case/punctuation aside), or one holds another."""
    norm = [re.sub(r"\W+", " ", str(o).lower()).strip() for o in options or []]
    return any(a and b and (a == b or (len(a) > 8 and (a in b or b in a)))
               for i, a in enumerate(norm) for b in norm[i + 1:])


def unfit_for_paper(q: dict, marks: int) -> bool:
    """An AI-written paper question that cannot go on a printed paper as it is."""
    return bool(references_missing_figure(q.get("text")) or re.search(UNSEEN_TEXT_REF, q.get("text") or "", re.I)
                or asks_too_much_for_marks(q.get("text"), marks) or has_unusable_options(q.get("options")))


def has_figure():
    """The question itself carries a picture (not only its worked solution)."""
    return _figure(_SHOWN)


def text_only_filter(*, pictures: bool = False):
    """Questions the app can show: no figure on the question (one only in the worked
    solution is fine), no reference to a figure the import couldn't attach, no blank MCQ
    option, a key that can be trusted (sound_key). `pictures`: also ones with a figure the
    screen can draw - inline SVG, or an imported image file (a "blob") on the question
    itself when ASSET_BASE_URL serves them; never a picture per option."""
    shown = _figure(_SHOWN)
    if get_settings().asset_base_url:
        drawable = not_(_figure('exists(@.blob) && @.role == "option"'))
    else:
        drawable = not_(_figure(f'exists(@.blob) && {_SHOWN}'))
    pictured = and_(shown, drawable) if pictures else false()
    unattached = DatasetQuestion.text.op("~*")(FIGURE_REF)
    return and_(
        or_(DatasetQuestion.figures.is_(None), not_(shown), pictured),
        or_(pictured, not_(unattached)),
        or_(DatasetQuestion.options.is_(None),
            not_(DatasetQuestion.options.op("@?")(literal_column(
                """'$[*] ? (@.type() == "string" && @ like_regex "^[[:space:]]*$")'::jsonpath""")))),
        sound_key(),
    )


_NOTE = r"marking scheme|another question"


def sound_key():
    """Questions whose key can be trusted: no marker's note left in it ("Marking scheme
    indicates ... for another question"), and no MCQ whose solution opens with another
    option ("(c) A is true but R is false." under the answer "A is false but R is true.")."""
    letter = func.substring(DatasetQuestion.solution, r"^\s*\(([a-dA-D])\)")
    named = DatasetQuestion.options.op("->>")(func.ascii(func.lower(letter)) - 97)
    rest = func.btrim(func.substring(DatasetQuestion.solution, r"^\s*\([a-dA-D]\)\s*([^\n]*)"))
    clash = and_(DatasetQuestion.question_type == QuestionType.MCQ, letter.is_not(None),
                 named.is_distinct_from(func.btrim(DatasetQuestion.answer)),
                 rest.is_distinct_from(func.btrim(DatasetQuestion.answer)),
                 func.lower(func.coalesce(func.substring(DatasetQuestion.answer, r"^\s*\(?([a-dA-D])\)"), ""))
                 != func.lower(letter))
    return and_(not_(DatasetQuestion.answer.op("~*")(_NOTE)),
                not_(func.coalesce(DatasetQuestion.solution, "").op("~*")(_NOTE)), not_(clash))


def sentences(text: str) -> set[str]:
    """Normalised sentences of a question, long enough to mean the same sub-question
    ("Dry HCl gas does not change the colour of dry litmus paper. Why?") wherever it sits."""
    return {key for part in re.split(r"(?<=[.?!])\s+|\n+", text or "")
            if len(key := re.sub(r"\W+", "", _PART.sub("", part).lower())) >= 30}


_STOP = frozenset("what which that this with from when does about following these those their there into have been "
                  "will would should could because while where than then them they your question catalog choose "
                  "correct option options answer find value given state write explain".split())


def words_of(text: str) -> frozenset[str]:
    """Content words of a stem: long non-stopwords, and every number (so 'question 1' and 'question 2' differ)."""
    return frozenset(w for w in re.findall(r"[a-z0-9]+", text.lower())
                     if w.isdigit() or (len(w) > 3 and w not in _STOP))


def near_duplicate(text: str, seen: list[frozenset[str]]) -> bool:
    """True when a stem is the same question as one already picked: at least 5 shared content words,
    at least 80% of the shorter stem's, and the same numbers (C-012: three differently worded
    "what does silver chloride do in sunlight" questions in one quiz; two questions that only share
    'Which of the following' or 'Find the value of x' are not). ponytail: word overlap, not meaning."""
    mine = words_of(text)
    nums = {w for w in mine if w.isdigit()}
    for other in seen:
        shared = len(mine & other)
        if (shared >= 5 and shared >= 0.8 * min(len(mine), len(other))
                and nums == {w for w in other if w.isdigit()}):
            return True
    return False


_PART = re.compile(r"^\s*\(?(?:[a-hA-H]|[ivx]{1,4}|\d{1,2})[.)]\s*")  # "(b) " / "ii. " / "3) " before a sub-question


def figure_svg(question: DatasetQuestion | None) -> str | None:
    """The question's inline picture (SVG markup), if it has one."""
    for figure in (question.figures or []) if question is not None else []:
        if figure.get("svg"):
            return figure["svg"]
    return None


def figure_url(question: DatasetQuestion) -> str | None:
    """Public URL of the question's own image file (not a solution or option one)."""
    for figure in question.figures or []:
        if figure.get("blob") and figure.get("role") not in ("solution", "option"):
            return asset_url(figure["blob"])
    return None


def figure_caption(question: DatasetQuestion) -> str | None:
    """What the question's own image shows (the import's caption) - alt text and answer-key context."""
    for figure in question.figures or []:
        if figure.get("caption") and figure.get("role") not in ("solution", "option"):
            return figure["caption"]
    return None


async def figure_svgs(session: AsyncSession, question_ids, *, captions: bool = False) -> dict[uuid.UUID, dict]:
    """{bank question id: {"figure_svg": inline picture, "figure_url": image file URL}}
    for those of `question_ids` with a picture to show - spread into the item out.
    `captions`: also "figure_caption" (for outs that have the field)."""
    ids = {i for i in question_ids if i is not None}
    if not ids:
        return {}
    rows = (await session.execute(select(DatasetQuestion).where(
        DatasetQuestion.id.in_(ids), DatasetQuestion.figures.is_not(None)))).scalars()
    out = {q.id: {"figure_svg": figure_svg(q), "figure_url": figure_url(q),
                  **({"figure_caption": figure_caption(q)} if captions else {})} for q in rows}
    return {k: v for k, v in out.items() if v["figure_svg"] or v["figure_url"]}


_DIFFICULTY_ORDER = [PaperDifficultyLevel.EASY, PaperDifficultyLevel.MEDIUM, PaperDifficultyLevel.HARD]
_CONFIDENCE_RANK = {"high": 0, "medium": 1, "low": 2}


@dataclass(frozen=True)
class Slot:
    bloom_level: BloomLevel
    difficulty: PaperDifficultyLevel


@dataclass
class Selection:
    picked: list[tuple[Slot, DatasetQuestion]] = field(default_factory=list)
    unfilled: list[Slot] = field(default_factory=list)
    # one entry per requested slot, in the same order: the question, or None if unfilled
    by_slot: list[DatasetQuestion | None] = field(default_factory=list)


def plan_slots(
    total: int, bloom_weights: dict[BloomLevel, int], difficulty: HomeworkDifficulty
) -> list[Slot]:
    """`bloom_weights` may be counts or percentages - either way they're
    weights, split over `total` by largest remainder. Slots come back
    ordered easy -> hard, so a homework ramps up."""
    if total <= 0:
        return []
    weights = {level: value for level, value in bloom_weights.items() if value > 0}
    if not weights:
        weights = {BloomLevel.UNDERSTAND: 1}
    whole = sum(weights.values())
    shares = {level: total * value / whole for level, value in weights.items()}
    counts = {level: int(share) for level, share in shares.items()}
    for level in sorted(shares, key=lambda lv: shares[lv] - counts[lv], reverse=True)[: total - sum(counts.values())]:
        counts[level] += 1

    # Bloom levels interleaved (R, U, A, R, U, A, ...) so a MIXED difficulty
    # spread lands on every level, not just the one with the most slots.
    remaining = {level: counts.get(level, 0) for level in BloomLevel}  # enum order = remember -> create
    blooms = []
    while any(remaining.values()):
        for level in BloomLevel:
            if remaining[level]:
                blooms.append(level)
                remaining[level] -= 1
    if difficulty == HomeworkDifficulty.EASY:
        difficulties = [PaperDifficultyLevel.EASY] * total
    elif difficulty == HomeworkDifficulty.HARD:
        difficulties = [PaperDifficultyLevel.HARD] * total
    else:  # MIXED: ~40% easy, 40% medium, 20% hard, evenly spaced so each Bloom level gets a mix
        hard = round(total * 0.2)
        easy = round((total - hard) / 2)
        want = {PaperDifficultyLevel.EASY: easy, PaperDifficultyLevel.MEDIUM: total - hard - easy,
                PaperDifficultyLevel.HARD: hard}
        given = dict.fromkeys(want, 0)
        difficulties = []
        for step in range(1, total + 1):  # pick whichever difficulty is furthest behind its share
            pick = max(want, key=lambda d: want[d] * step / total - given[d])
            given[pick] += 1
            difficulties.append(pick)
    slots = [Slot(level, level_difficulty) for level, level_difficulty in zip(blooms, difficulties)]
    return sorted(slots, key=lambda s: (_DIFFICULTY_ORDER.index(s.difficulty), list(BloomLevel).index(s.bloom_level)))


async def select_questions(
    session: AsyncSession,
    *,
    dataset_ids: list[uuid.UUID | str],
    chapter_id: uuid.UUID | str,
    slots: list[Slot],
    topic_id: uuid.UUID | str | None = None,
    subtopic_id: uuid.UUID | str | None = None,
    exclude_uids: set[str] | frozenset[str] = frozenset(),
    seed: int | str | None = None,
    text_only: bool = False,
    pictures: bool = False,
    focus_concepts: set[str] | frozenset[str] = frozenset(),
) -> Selection:
    """Fill `slots` from the chapter's questions in `dataset_ids`.

    Per slot the best candidate is, in order: closest scope (the subtopic,
    then the rest of its topic, then the rest of the chapter), difficulty
    closest to the slot's (at most one step off, and only within the gap's
    subtopic/topic - a HARD slot never gets an EASY question, nor an
    unrelated MEDIUM one from elsewhere in the chapter), least concept overlap with what's already picked, most
    confident curriculum link, most often asked in past papers, then a
    seeded shuffle so two homeworks on the same gap aren't identical.
    `text_only` skips questions that need a figure (for clients that can't
    show images yet) - with `pictures`, drawn (inline SVG) pictures are kept. `focus_concepts` (the student's weak concepts) ranks a
    question covering any of them above everything else.
    """
    selection = Selection()
    if not dataset_ids or not slots:
        selection.unfilled = list(slots)
        selection.by_slot = [None] * len(slots)
        return selection

    query = select(DatasetQuestion).where(
        DatasetQuestion.dataset_id.in_([str(d) for d in dataset_ids]),
        DatasetQuestion.chapter_id == str(chapter_id),
        DatasetQuestion.bloom_level.in_({s.bloom_level for s in slots}),
    )
    if text_only:
        query = query.where(text_only_filter(pictures=pictures))
    rows = (await session.execute(query)).scalars().all()

    # One candidate per question: the same question sits in several datasets.
    candidates: dict[str, DatasetQuestion] = {}
    for row in rows:
        key = row.external_uid or str(row.id)
        if key not in exclude_uids:
            candidates.setdefault(key, row)

    rng = random.Random(str(seed) if seed is not None else None)
    tiebreak = {key: rng.random() for key in candidates}
    subtopic, topic = (str(subtopic_id) if subtopic_id else None), (str(topic_id) if topic_id else None)

    def scope_rank(q: DatasetQuestion) -> int:
        if subtopic and str(q.subtopic_id) == subtopic:
            return 0
        if topic and str(q.topic_id) == topic:
            return 1
        return 2

    focus = {c.lower() for c in focus_concepts}
    used: set[str] = set()
    used_concepts: set[str] = set()
    used_sentences: set[str] = set()  # one sub-question shared by two bank questions only once
    used_words: list[frozenset[str]] = []  # C-014: nor two questions worded alike
    for slot in slots:
        target = _DIFFICULTY_ORDER.index(slot.difficulty)
        best_key, best_rank = None, None
        for key, q in candidates.items():
            if (key in used or q.bloom_level != slot.bloom_level or sentences(q.text) & used_sentences
                    or near_duplicate(q.text, used_words)):
                continue
            distance = abs(_DIFFICULTY_ORDER.index(q.difficulty) - target) if q.difficulty else 1
            scope = scope_rank(q)
            # one step off is fine on the gap's own subtopic/topic; from elsewhere in the
            # chapter only an exact difficulty match is worth more than generating one
            if distance > 1 or (distance == 1 and scope == 2):
                continue
            enriched = (q.source or {}).get("enriched", {})
            concepts = {c.lower() for c in q.concepts or []}
            rank = (
                0 if focus & concepts else 1,
                scope,
                distance,
                len(used_concepts & concepts),
                _CONFIDENCE_RANK.get(enriched.get("topic_confidence"), 1),
                -len(q.years_asked or []),
                tiebreak[key],
            )
            if best_rank is None or rank < best_rank:
                best_key, best_rank = key, rank
        if best_key is None:
            selection.unfilled.append(slot)
            selection.by_slot.append(None)
            continue
        used.add(best_key)
        chosen = candidates[best_key]
        used_concepts |= {c.lower() for c in chosen.concepts or []}
        used_sentences |= sentences(chosen.text)
        used_words.append(words_of(chosen.text))
        selection.picked.append((slot, chosen))
        selection.by_slot.append(chosen)
    return selection
