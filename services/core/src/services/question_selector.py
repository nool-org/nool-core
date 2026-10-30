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

import math
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
_ENUMERATORS = re.compile(r"\(\s*(?:[a-e]|i{1,3}|iv)\s*\)", re.I)


def asks_too_much_for_marks(text: str | None, marks: int) -> bool:
    """T-070: a 1-3 mark question with more parts than marks allow ("a 2-mark question with about 6 answers")."""
    # T-028: "(i) ... and (ii) ...?" is two parts, not three: the "?" closes the parts already counted, so take the larger count
    parts = max(len(_ENUMERATORS.findall(text or "")), (text or "").count("?"))
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


def _answer_key(answer: str | None) -> str:
    key = re.sub(r"\W+", " ", str(answer or "").lower()).strip()
    return key if 0 < len(key) <= 60 else ""  # a paragraph answer is not an identity


def same_question(a: tuple, b: tuple) -> bool:
    """T-070: two (text, options, answer) are the same question for a paper when the stems are near-identical, or
    they have the same short answer and share the stem's concept words ("kilowatt hour" asked twice, worded two ways),
    or they offer the same options about the same thing. ponytail: word overlap, not meaning."""
    (ta, oa, aa), (tb, ob, ab) = a, b
    if re.sub(r"\W+", "", ta.lower()) == re.sub(r"\W+", "", tb.lower()) or near_duplicate(ta, [words_of(tb)]):
        return True
    wa, wb = words_of(ta), words_of(tb)
    shared = {w for w in wa & wb if not w.isdigit()}
    if (key := _answer_key(aa)) and key == _answer_key(ab) and len(shared) >= 3 and len(shared) >= 0.3 * len(wa | wb):
        return True
    options = lambda o: {re.sub(r"\W+", " ", str(x).lower()).strip() for x in o or []}  # noqa: E731
    return len(options(oa) & options(ob)) >= 3 and len(shared) >= 2


def duplicates_of(items: list[tuple]) -> dict[int, int]:
    """{index of a question: index of the earlier question it repeats} for a paper's (text, options, answer) list."""
    return {j: next(i for i in range(j) if same_question(items[i], items[j]))
            for j in range(len(items)) if any(same_question(items[i], items[j]) for i in range(j))}


MAX_PER_CONCEPT = 2
_ALIAS = {"detergent": "soap", "detergents": "soap", "soaps": "soap", "micelles": "micelle"}
_TOPIC = frozenset("light current electric electricity energy number solution equation reaction chemical metal metals "
                   "value figure diagram related following statement correct incorrect true false based formula "
                   "produced produce used uses calculate shown below given using called known nature property "
                   # B28: generic words that are in every stem, never an idea of their own
                   "student students formed form forms body human connected connect explain describe write state "
                   "name list give example examples help helps case cases system process place different "
                   "similar common effect effects reason reasons difference differences define situation "
                   "observed observe observation experiment activity result results change changes things thing "
                   "people person water air house household home daily everyday general important main major "
                   "several certain various following along between during while which what why how "
                   "where when would could should about after before through other another their there these those "
                   "points point part parts type types kind kinds mark marks answer question questions".split())


def _concepts(text: str) -> set[str]:
    """T-070 concept words of a stem: long content words, plural folded, soap/detergent one idea."""
    out = set()
    for w in words_of(text):
        if not w.isdigit():
            w = _ALIAS.get(w, w)
            w = w[:-1] if w.endswith("s") and not w.endswith("ss") else w
            if w not in _TOPIC:
                out.add(w)
    return out


def crowded(texts: list[str], total: int | None = None, universe=(), terms=None) -> dict[int, str]:
    """T-070: {index: concept} for each question that is a 3rd+ use of a concept word on the paper (cap MAX_PER_CONCEPT).
    A word in over half the questions is the chapter itself, not a repeated idea; while a paper is still being built,
    `universe` (the other questions on offer) shows how common a word is. `terms` (B28): when given, only a word in it
    (the chapters' topic words) counts as an idea. ponytail: word overlap."""
    sets = [_concepts(t) for t in texts]
    wide = [_concepts(u) for u in universe] or sets  # the whole offering when known (not the half-built paper)
    n = max(total or 0, len(texts))
    if n < 8:
        return {}
    seen: dict[str, int] = {}
    out: dict[int, str] = {}
    for i, words in enumerate(sets):
        for w in sorted(words):
            seen[w] = seen.get(w, 0) + 1
            if (seen[w] > MAX_PER_CONCEPT and (terms is None or w in terms) and i not in out
                    and sum(w in x for x in wide) <= len(wide) / 2):
                out[i] = w
    return out


def concept_full(text: str, others: list[str], total: int | None = None, universe=()) -> bool:
    """Adding `text` to a paper holding `others` would be a 3rd use of some concept."""
    return len(others) in crowded([*others, text], total, universe)


def topic_full(key, keys: list, total: int) -> bool:
    """B28: a paper of 8+ questions keeps a quarter of its questions (at least 3) on one subtopic at most."""
    return bool(key) and total >= 8 and keys.count(key) >= max(3, math.ceil(total / 4))


def over_marked(text: str | None, marks: int, bloom) -> bool:
    """T-070: a Remember question worth 3+ marks that is not multi-part (one fact cannot earn that much)."""
    return (str(getattr(bloom, "value", bloom)) == "REMEMBER" and marks > 2
            and max(len(_ENUMERATORS.findall(text or "")), (text or "").count("?")) < 2)


_RECALL_STEM = re.compile(r"^\s*(which|what|name|state|define|identify|write|the)\b", re.I)
_REASONING = re.compile(r"\b(because|why|justify|best|most (appropriate|suitable|likely)|compare|evaluate|assess|predict|"
                        r"design|suggest|would|should|if|conclude|infer|reason|scenario)\b", re.I)


def plain_recall(text: str | None, marks: int) -> bool:
    """T-070: a 1-mark question that just asks for a fact - it cannot honestly be labelled Evaluate or Create."""
    return marks <= 1 and bool(_RECALL_STEM.match(text or "")) and not _REASONING.search(text or "") and len((text or "").split()) < 30


_PART = re.compile(r"^\s*\(?(?:[a-hA-H]|[ivx]{1,4}|\d{1,2})[.)]\s*")  # "(b) " / "ii. " / "3) " before a sub-question


_SEGMENT = re.compile(r'<line x1="([\d.]+)" y1="([\d.]+)" x2="([\d.]+)" y2="([\d.]+)" stroke="#2D3748" stroke-width="2"/>')
_LABEL = re.compile(r'<text x="([\d.]+)" y="([\d.]+)"([^>]*?font-size="(\d+)"[^>]*)>([^<]+)</text>')


def _smooth_curves(svg: str) -> str:
    """C-022: the bank draws a graph as separate straight <line> pieces between its sample points (a zig-zag). A run
    of 4+ pieces that join end to end and always move right is one curve: redraw it as a smooth Catmull-Rom path
    through the same points. ponytail: through the bank's own points, no re-sampling of the function."""
    def curve(run):
        pts = [(float(run[0][0]), float(run[0][1])), *((float(a[2]), float(a[3])) for a in run)]
        d = f"M {pts[0][0]} {pts[0][1]}"
        for i in range(len(pts) - 1):
            p0, p1, p2, p3 = pts[max(i - 1, 0)], pts[i], pts[i + 1], pts[min(i + 2, len(pts) - 1)]
            c1 = (p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6)
            c2 = (p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6)
            d += f" C {c1[0]:.1f} {c1[1]:.1f} {c2[0]:.1f} {c2[1]:.1f} {p2[0]} {p2[1]}"
        return f'<path d="{d}" fill="none" stroke="#2D3748" stroke-width="2" stroke-linecap="round"/>'

    edits, run = [], []  # (start, end, replacement); the pieces may have labels and dots between them in the markup

    def flush():
        nonlocal run
        if len(run) >= 4:
            edits.extend((m[4], m[5], "") for m in run[1:])
            edits.append((run[0][4], run[0][5], curve([m[:4] for m in run])))
        run = []
    for m in _SEGMENT.finditer(svg):
        g = m.groups()
        if not (run and g[:2] == run[-1][2:4] and float(g[2]) > float(g[0])):
            flush()
        run.append((*g, m.start(), m.end()))
    flush()
    for start, end, new in sorted(edits, reverse=True):
        svg = svg[:start] + new + svg[end:]
    return svg


def _separate_labels(svg: str) -> str:
    """C-022: labels whose boxes overlap (two zeroes next to each other) - move the later one down, clear of the earlier."""
    placed: list[tuple[float, float, float, float]] = []

    def fix(m):
        x, y, size = float(m[1]), float(m[2]), int(m[4])
        w, h = 0.6 * size * len(m[5].strip()), size
        for _ in range(4):
            hit = next((b for b in placed if abs(x - b[0]) < (w + b[2]) / 2 and abs(y - b[1]) < (h + b[3]) / 2), None)
            if hit is None:
                break
            y = hit[1] + (h + hit[3]) / 2 + 1
        placed.append((x, y, w, h))
        return f'<text x="{m[1]}" y="{y:.1f}"{m[3]}>{m[5]}</text>'
    return _LABEL.sub(fix, svg)


def figure_svg(question: DatasetQuestion | None) -> str | None:
    """The question's inline picture (SVG markup), if it has one - every screen and the PDF come through here."""
    for figure in (question.figures or []) if question is not None else []:
        if figure.get("svg"):
            return _separate_labels(_smooth_curves(figure["svg"]))
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
