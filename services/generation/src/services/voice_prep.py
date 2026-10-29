"""Voice-test question prep: reword the dataset questions core picked for a
voice test so Milo can say them aloud in simple Indian English, and write a
per-question rubric (expected points, misconceptions, two hints) for the
grader. Same LlamaIndex Workflow shape as gap_fill.py:

    start -> generate -> verify --(failed items, once)--> generate -> verify -> stop

- generate: one GENERATION_MODEL call per question, in parallel.
- verify: code checks first (sentence length, no changed/invented numbers,
  kept numbers keep their units, no banned Western/idiomatic words, no
  "write/draw/tabulate/any four" asks, rubric shape), then one VERIFY_MODEL
  call for the batch that answers each reworded question itself and compares
  it with the dataset answer, and checks concept, Bloom level, difficulty and
  that it works when heard once and answered by speaking.
- an item still failing after the retry keeps its ORIGINAL wording
  (reworded_text = original_text, spoken_text derived in code) and, if its
  last draft had a well-formed rubric, that rubric. Core treats an unchanged
  reworded_text as "could not be made spoken" and drops the question.
- every question also gets an `easier` version (same concept, one notch more
  supported) from the same call, held to the same code checks and checked by
  the verifier; a failing easier version comes back null - never sinking the
  question itself.

Stateless: core sends the candidates already chosen per Bloom level.
"""

import asyncio
import json
import logging
import re

from llama_index.core.workflow import Context, Event, StartEvent, StopEvent, Workflow, step
from pydantic import BaseModel, Field, model_validator

from src.services.gap_fill import BloomLevel, Difficulty
from src.services.llm import StructuredLLM

logger = logging.getLogger(__name__)

MAX_SENTENCE_WORDS = 18  # the rule says "about 15"; a little slack so a good rewording isn't thrown away
MAX_QUESTION_WORDS = 30  # the whole spoken question; the rule says about 20 - slack for a numbers set-up
LEAK_WORDS = 5  # a hint sharing this many words in a row with an expected point gives the answer away
MAX_TEXT = 4000

# Western / idiomatic words a Class 8-10 student in a South Indian classroom
# wouldn't hear in class. Only flagged when the original didn't already use them.
BANNED_WORDS = (
    "gonna", "wanna", "kinda", "gotta", "folks", "guys", "dude", "buddy", "awesome", "y'all",
    "piece of cake", "ballpark", "in a nutshell", "hit the nail", "rule of thumb", "bottom line", "figure out",
    "gas station", "gasoline", "dollar", "dollars", "cents", "faucet", "flashlight", "apartment", "elevator",
    "soccer", "baseball", "fahrenheit", "mph", "cookie", "candy", "sophomore", "freshman",
)


# ---- request / result ----------------------------------------------------
class Candidate(BaseModel):
    id: str = Field(max_length=100)
    text: str = Field(min_length=1, max_length=MAX_TEXT)
    answer: str = Field(min_length=1, max_length=MAX_TEXT)
    bloom_level: BloomLevel
    topic_id: str | None = Field(default=None, max_length=100)
    subtopic_id: str | None = Field(default=None, max_length=100)
    concepts: list[str] = Field(default_factory=list, max_length=20)
    difficulty: Difficulty | None = None
    instruction: str | None = Field(default=None, max_length=MAX_TEXT)  # e.g. a teacher's "make it clearer"


class PrepRequest(BaseModel):
    grade: int = Field(ge=1, le=12)
    subject: str = Field(max_length=200)
    chapter: str = Field(max_length=300)
    board: str = Field(default="CBSE", max_length=100)
    levels: list[BloomLevel] = Field(min_length=1, max_length=12)
    candidates: list[Candidate] = Field(min_length=1, max_length=12)

    @model_validator(mode="after")
    def _every_level_has_a_candidate(self):
        self.picked()
        return self

    def picked(self) -> list[Candidate]:
        """One candidate per requested level, in the order of `levels`: the
        first not-yet-used candidate with that Bloom level (extras ignored)."""
        used: set[int] = set()
        out = []
        for level in self.levels:
            i = next((i for i, c in enumerate(self.candidates) if i not in used and c.bloom_level == level), None)
            if i is None:
                raise ValueError(f"no candidate left for Bloom level {level}")
            used.add(i)
            out.append(self.candidates[i])
        return out


class EasierVersion(BaseModel):
    spoken_text: str
    expected_points: list[str]
    misconceptions: list[str]
    hints: list[str]


class PreppedQuestion(BaseModel):
    order: int
    dataset_question_id: str
    bloom_level: BloomLevel
    topic_id: str | None
    subtopic_id: str | None
    concepts: list[str]
    original_text: str
    reworded_text: str
    spoken_text: str
    expected_points: list[str]
    misconceptions: list[str]
    hints: list[str]
    easier: EasierVersion | None = None


class PrepResult(BaseModel):
    questions: list[PreppedQuestion]


# ---- LLM output shapes ---------------------------------------------------
class _Version(BaseModel):
    reworded_text: str = Field(description="the question asked a new way for speech")
    spoken_text: str = Field(description="reworded_text as Milo says it aloud: formulas and symbols in words")
    expected_points: list[str] = Field(description="2-4 points a full answer must contain (2-3 when easier)")
    misconceptions: list[str] = Field(description="2-3 common wrong ideas students have on this question")
    hints: list[str] = Field(description="exactly 2: point to the key idea, then a smaller sub-question; never the answer")


class _Rewording(_Version):
    easier: _Version | None = Field(description="the same question one notch more supported, for a stuck student")


class _Verdict(BaseModel):
    index: int
    answer_to_reworded: str = Field(description="your own short answer to the reworded question")
    ok: bool
    problem: str = Field(description="empty when ok; otherwise what is wrong, specifically")
    easier_ok: bool = Field(description="the easier version passes check E; false when there is none")


class _Verdicts(BaseModel):
    verdicts: list[_Verdict]


GENERATE_SYSTEM = """You prepare a {board} Class {grade} {subject} question for a spoken voice test.
A friendly co-learner will read it aloud to an Indian school student, in a South Indian classroom.

Reword the question so it works as a SPOKEN question. Keep the SAME concept, the SAME Bloom level
and difficulty, and an answer that agrees with the given answer. Never add a part (no extra
"give reason", no extra sub-question).
The student hears it ONCE and answers by speaking ONCE:
- It is answered by speaking: no sub-parts, no lists, no dialogue or story set-up, nothing to write, draw
  or calculate at length.
- Never say "write", "draw", "list" or "tabulate". Turn "write/state/list any four (or N) ..." into
  "Tell me two or three ...". Asking for fewer items is fine.
- Never ask the student to say an equation or a formula, even with "say" or "tell": ask what is formed,
  what changes, or why (spoken equations can't be followed or graded).
- A long multi-step calculation becomes a reasoning ask ("How would you find ...?") or one simple step.
- Self-contained: never say "this lens", "the given circuit", "the above" without first saying what it is.
- Simple Indian English, as a teacher speaks in a South Indian classroom.
- Short sentences, about 15 words or fewer each. Everyday words.
- ONE question asking ONE thing, about 20 words in all and never more than 30. Never two questions joined
  ("... ? And how ...?"), never a chain of asks ("Define ... Give ... Also say ...", "Then explain ...").
  If the original asks two things, ask the more important one.
- No idioms, no slang, no American or British phrasing.
- Keep textbook terms exactly as in NCERT/state-board books (for example potential difference, resistivity).
- Keep a number only if the question still needs it; never change a number or add a new one.
  Every number you keep keeps its unit and symbol, written the same way. Say counts in words ("two or three").
- Only if the original already has a scenario, you may set it in a familiar Indian setting
  (ceiling fan, mixie, tube light, geyser, plug point, cricket, bus, rupees). Never add a scenario.
- Use phrasing students hear in class: "two times", "what will happen", "why is it like that".
- Never refer to a figure, diagram or table. Drop marks like "(2)" or "[3 marks]".

Also write:
- spoken_text: reworded_text exactly as it should be said aloud. Formulas and symbols in words
  ("V = IR" -> "V equals I times R", "2 Ω" -> "2 ohm", "m/s" -> "metre per second", "x²" -> "x squared"; chemical symbols as names: "Zn" -> "zinc").
- expected_points: 2 to 4 short points, taken from the given answer, for the question AS REWORDED.
  Each is one fact or reasoning step from the answer - never a note to the marker ("any two are enough").
  If it now asks for two or three items, list the main ones (the grader reads "two or three" from the question);
  if it asks how to find something, the points are the steps of reasoning, not the full calculation.
- misconceptions: 2 or 3 common wrong ideas students of this class have about this question.
- hints: exactly 2, a nudge ladder. Hint 1 points the student to the key concept or idea to think
  about, without naming the answer. Hint 2 is a smaller, easier sub-question that leads toward it.
  A hint never states the answer or any part of it; for a multi-part question, not even one part's answer.
  Each hint is a short question ending in "?" (like "What happens to rays that hit a curved-in mirror?").
- easier: the SAME question one notch more supported, for a student who is stuck. Same concept and
  topic; simpler words and a more concrete ask; it may be one Bloom level lower. Every rule above
  applies to it too. It has its own reworded_text, spoken_text, expected_points (2 or 3),
  misconceptions (2 or 3) and hints (exactly 2, the same nudge ladder).
If `instruction` is given, follow it.
If `feedback` is given, your previous attempt was rejected for that reason - fix it."""

VERIFY_SYSTEM = """You are a strict {board} Class {grade} {subject} teacher checking reworded voice-test questions.
For each item: first answer the REWORDED question yourself, briefly, in `answer_to_reworded`.
Then ok=true only if ALL hold:
1. Your answer agrees with the dataset answer (same facts; for a narrowed ask, a correct part of it).
2. It asks about the same concept at the same thinking level (Bloom level) and difficulty as the original.
   It may ask for fewer items, or for the reasoning instead of a long calculation; it adds no new part.
3. Self-contained when heard aloud once: anything it refers to ("this lens", "the circuit") is introduced
   in the question itself; no figure, diagram, table or "given above".
4. Answerable by speaking: nothing to write, draw or tabulate, no long list of items, no long working.
5. spoken_text says the same thing as reworded_text, with symbols and formulas in words.
6. expected_points agree with the dataset answer and match what the REWORDED question asks.
7. No hint states the answer or any part of it (for a multi-part question, any one part's answer counts).
   Hint 1 points to the key idea; hint 2 is a smaller sub-question that leads toward the answer.
   A hint that tells the student what happens (a statement of the fact the question asks for) fails.
8. It asks ONE thing - not two questions in one, and not a chain of asks ("Define ... Give ... Also say ...").
`ok` is about the reworded question only.
E. Separately, easier_ok=true only if the `easier` version asks about the same concept and topic, is simpler
   (it may be one Bloom level lower), meets 3-8 above, and its expected_points agree with the dataset answer
   for what it asks.
If not ok, say precisely what is wrong in `problem`."""


# ---- code checks ---------------------------------------------------------
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_NOT_UNITS = {"a", "an", "and", "or", "is", "are", "was", "the", "of", "to", "in", "on", "at", "for", "by",
              "each", "then", "so", "if", "as", "with", "from", "into", "than", "more", "less", "times"}
_UNIT_AFTER_NUMBER = re.compile(r"(\d+(?:\.\d+)?)\s*([^\s\d.,;:?!()\[\]]+)")


def _units(text: str) -> set[str]:
    """Number + the unit written after it: 220 V, 2 Ω, 10 m/s."""
    # ponytail: "short token after a number" ~ unit; long words ("resistors") and _NOT_UNITS are skipped
    return {f"{n} {u}" for n, u in _UNIT_AFTER_NUMBER.findall(text)
            if (len(u) <= 4 or not u.isalpha()) and u not in _NOT_UNITS}  # case-sensitive: "A" is ampere


# asks a spoken answer can't meet - the prompt turns these into "tell me two or three ..."
_WRITTEN_ASK = re.compile(r"\b(?:write|draw|tabulate|equations?|formulae?)\b|\bany (?:three|four|five|six)\b", re.IGNORECASE)


def _has_word(text: str, word: str) -> bool:
    return re.search(rf"(?<![\w']){re.escape(word)}(?![\w'])", text, re.IGNORECASE) is not None


_MARKS = re.compile(r"[(\[]\s*\d+\s*(?:marks?)?\s*[)\]]\s*$", re.IGNORECASE)  # "(2)", "[3 marks]" at the end


def _problems(original: str, draft: _Version, max_points: int = 4) -> str | None:
    """Everything that can be checked without a model; None when clean."""
    original = _MARKS.sub("", original.strip())
    text = draft.reworded_text.strip()
    if not text or not draft.spoken_text.strip():
        return "reworded_text or spoken_text is empty"
    # a line break ends a sentence too (rewordings put each sub-question / clause on its own line)
    long = [s for s in re.split(r"(?<=[.?!])\s+|\n+", text) if len(s.split()) > MAX_SENTENCE_WORDS]
    if long:
        return f"sentence longer than {MAX_SENTENCE_WORDS} words, split it: {long[0]!r}"
    if draft.spoken_text.count("?") > 1:
        return "ask ONE question only - it has more than one '?'; keep the more important one"
    if len(draft.spoken_text.split()) > MAX_QUESTION_WORDS:
        return f"too long to hold in mind when heard once: about 20 words, at most {MAX_QUESTION_WORDS}, ask one thing"
    numbers = set(_NUMBER.findall(text))
    added = numbers - set(_NUMBER.findall(original))
    if added:
        return f"never change or add a number (say counts in words); not in the original: {', '.join(sorted(added))}"
    # ponytail: a dropped number is allowed (the ask may narrow); the verifier checks it is still answerable
    missing = {u for u in _units(original) if u.split()[0] in numbers} - _units(text)
    if missing:
        return f"a number you keep keeps its unit and symbol as written; missing: {', '.join(sorted(missing))}"
    if m := _WRITTEN_ASK.search(f"{text} {draft.spoken_text}"):
        return f"it is answered by speaking once: do not say {m.group(0)!r}; ask for two or three things, said aloud"
    banned = [w for w in BANNED_WORDS if _has_word(text, w) and not _has_word(original, w)]
    if banned:
        return f"use simple Indian classroom English; avoid: {', '.join(banned)}"
    return _rubric_problem(draft, max_points)


def _without_bad_easier(original: str, draft: _Rewording) -> _Rewording:
    """The same code checks for the easier version; a failing one is dropped, not the question."""
    if draft.easier is None or _problems(original, draft.easier, max_points=3) is None:
        return draft
    return draft.model_copy(update={"easier": None})


def _rubric_problem(draft: _Version, max_points: int = 4) -> str | None:
    if not 2 <= len(_points(draft.expected_points)) <= max_points:
        return f"expected_points must have 2 to {max_points} items"
    if not 2 <= len(_clean(draft.misconceptions)) <= 3:
        return "misconceptions must have 2 or 3 items"
    if len(_clean(draft.hints)) != 2:
        return "hints must have exactly 2 items"
    if statement := next((h for h in _clean(draft.hints) if not h.endswith("?")), None):
        return f"each hint is a guiding question ending in '?', never a statement: {statement!r}"
    if leak := next((h for h in _clean(draft.hints) if _gives_away(h, draft.expected_points)), None):
        return f"this hint gives away part of the answer - ask a guiding question instead: {leak!r}"
    return None


def _runs(text: str) -> set[tuple]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {tuple(words[i:i + LEAK_WORDS]) for i in range(len(words) - LEAK_WORDS + 1)}


def _gives_away(hint: str, points: list[str]) -> bool:
    """The hint repeats LEAK_WORDS words in a row of an expected point - it says the answer."""
    return any(_runs(hint) & _runs(p) for p in points)


def _clean(items: list[str]) -> list[str]:
    return [s.strip() for s in items if s.strip()]


# a marking note ("Any two or three of these are enough.") the model adds to the points, alone or at the end of one
_MARKING_NOTE = re.compile(r"\s*\bany (?:one|two|three|four|\d+)(?: or (?:two|three|four|\d+))?(?: of (?:these|them))?"
                           r" (?:is|are) enough\b\.?\s*$", re.IGNORECASE)


def _points(items: list[str]) -> list[str]:
    """Expected points without marking notes - students see every point as an idea they may have missed."""
    return _clean([_MARKING_NOTE.sub("", s) for s in items])


_SPOKEN = [  # order matters: longer symbols first
    ("°C", " degree Celsius"), ("°", " degree"), ("Ω", " ohm"), ("μ", " micro"), ("²", " squared"), ("³", " cubed"),
    ("√", " root of "), ("≈", " approximately equals "), ("→", " gives "), ("×", " times "), ("÷", " divided by "),
    ("=", " equals "), ("+", " plus "), (" - ", " minus "), ("−", " minus "), ("%", " percent"), ("^", " to the power "),
]


def spoken(text: str) -> str:
    """Symbols to words for the fallback (original wording) - the model does this better when it can."""
    for symbol, words in _SPOKEN:
        text = text.replace(symbol, words)
    return re.sub(r"\s+", " ", text).strip()


# ---- workflow ------------------------------------------------------------
class GenerateEvent(Event):
    indexes: list[int]
    attempt: int
    feedback: dict[int, str]


class VerifyEvent(Event):
    indexes: list[int]
    attempt: int
    drafts: dict[int, _Rewording]


class VoicePrepWorkflow(Workflow):
    def __init__(self, llm: StructuredLLM, *, generation_model: str, verify_model: str,
                 generation_effort: str = "low", verify_effort: str = "low", max_attempts: int = 2, **kwargs):
        super().__init__(**kwargs)
        self.llm = llm
        self.generation_model, self.verify_model = generation_model, verify_model
        self.generation_effort, self.verify_effort = generation_effort, verify_effort
        self.max_attempts = max_attempts

    @step
    async def start(self, ctx: Context, ev: StartEvent) -> GenerateEvent:
        request: PrepRequest = ev.request
        await ctx.store.set("request", request)
        await ctx.store.set("picked", request.picked())
        await ctx.store.set("accepted", {})
        await ctx.store.set("last_draft", {})
        return GenerateEvent(indexes=list(range(len(request.levels))), attempt=1, feedback={})

    @step
    async def generate(self, ctx: Context, ev: GenerateEvent) -> VerifyEvent:
        request: PrepRequest = await ctx.store.get("request")
        picked: list[Candidate] = await ctx.store.get("picked")
        system = GENERATE_SYSTEM.format(board=request.board, grade=request.grade, subject=request.subject)

        async def one(i: int) -> _Rewording | None:
            c = picked[i]
            payload = {
                "chapter": request.chapter,
                "question": c.text, "answer": c.answer, "bloom_level": c.bloom_level,
                **({"difficulty": c.difficulty} if c.difficulty else {}),
                **({"concepts": c.concepts} if c.concepts else {}),
                **({"instruction": c.instruction} if c.instruction else {}),
                **({"feedback": ev.feedback[i]} if i in ev.feedback else {}),
            }
            try:
                return await self.llm(model=self.generation_model, effort=self.generation_effort, system=system,
                                      user=json.dumps(payload, ensure_ascii=False), output_type=_Rewording)
            except Exception as exc:  # one item's failure mustn't sink the others
                logger.warning("voice_prep_generate_failed", extra={"item": i, "error": type(exc).__name__})
                return None

        results = await asyncio.gather(*(one(i) for i in ev.indexes))
        return VerifyEvent(indexes=ev.indexes, attempt=ev.attempt,
                           drafts={i: d for i, d in zip(ev.indexes, results) if d})

    @step
    async def verify(self, ctx: Context, ev: VerifyEvent) -> GenerateEvent | StopEvent:
        request: PrepRequest = await ctx.store.get("request")
        picked: list[Candidate] = await ctx.store.get("picked")
        accepted: dict[int, _Rewording] = await ctx.store.get("accepted")
        last_draft: dict[int, _Rewording] = await ctx.store.get("last_draft")
        last_draft.update(ev.drafts)
        feedback: dict[int, str] = {}
        clean: dict[int, _Rewording] = {}
        for i, d in ev.drafts.items():  # code checks first - no LLM needed to reject these
            problem = _problems(picked[i].text, d)
            if problem:
                feedback[i] = problem
            else:
                clean[i] = _without_bad_easier(picked[i].text, d)
        if clean:
            payload = {"items": [{
                "index": i, "original": picked[i].text, "dataset_answer": picked[i].answer,
                "bloom_level": picked[i].bloom_level, "difficulty": picked[i].difficulty, **d.model_dump(),
            } for i, d in clean.items()]}
            try:
                verdicts = await self.llm(
                    model=self.verify_model, effort=self.verify_effort,
                    system=VERIFY_SYSTEM.format(board=request.board, grade=request.grade, subject=request.subject),
                    user=json.dumps(payload, ensure_ascii=False), output_type=_Verdicts,
                )
            except Exception as exc:  # unverified drafts count as failed
                logger.warning("voice_prep_verify_failed", extra={"items": len(clean), "error": type(exc).__name__})
                verdicts = _Verdicts(verdicts=[])
            ok = {v.index for v in verdicts.verdicts if v.ok}
            easier_ok = {v.index for v in verdicts.verdicts if v.easier_ok}
            problems = {v.index: v.problem for v in verdicts.verdicts if not v.ok}
            for i, d in clean.items():
                if i in ok:
                    accepted[i] = d if i in easier_ok else d.model_copy(update={"easier": None})
                else:
                    feedback[i] = problems.get(i) or "not verified"
        failed = [i for i in ev.indexes if i not in accepted]
        for i in failed:
            feedback.setdefault(i, "nothing was produced for this question")
        await ctx.store.set("accepted", accepted)
        await ctx.store.set("last_draft", last_draft)

        if failed and ev.attempt < self.max_attempts:
            return GenerateEvent(indexes=failed, attempt=ev.attempt + 1, feedback=feedback)
        if failed:
            logger.info("voice_prep_fallback_to_original",
                        extra={"items": failed, "reasons": {i: feedback.get(i, "")[:200] for i in failed}})
        return StopEvent(result=PrepResult(questions=[
            _result(i, c, accepted.get(i), last_draft.get(i)) for i, c in enumerate(picked)]))


def _result(i: int, c: Candidate, accepted: _Rewording | None, last: _Rewording | None) -> PreppedQuestion:
    rubric = accepted or (last if last and _rubric_problem(last) is None else None)
    return PreppedQuestion(
        order=i + 1, dataset_question_id=c.id, bloom_level=c.bloom_level, topic_id=c.topic_id,
        subtopic_id=c.subtopic_id, concepts=c.concepts, original_text=c.text,
        reworded_text=accepted.reworded_text.strip() if accepted else c.text,
        spoken_text=accepted.spoken_text.strip() if accepted else spoken(_MARKS.sub("", c.text.strip())),
        expected_points=_points(rubric.expected_points) if rubric else [],
        misconceptions=_clean(rubric.misconceptions) if rubric else [],
        hints=_clean(rubric.hints) if rubric else [],
        easier=EasierVersion(
            spoken_text=accepted.easier.spoken_text.strip(), expected_points=_points(accepted.easier.expected_points),
            misconceptions=_clean(accepted.easier.misconceptions), hints=_clean(accepted.easier.hints),
        ) if accepted and accepted.easier else None,
    )


async def prep_questions(llm: StructuredLLM, request: PrepRequest, *, generation_model: str, verify_model: str,
                         timeout: float = 170.0) -> PrepResult:
    workflow = VoicePrepWorkflow(llm, generation_model=generation_model, verify_model=verify_model, timeout=timeout)
    return await workflow.run(request=request)
