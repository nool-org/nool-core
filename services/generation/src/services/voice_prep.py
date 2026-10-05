"""Voice-test question prep: reword the dataset questions core picked for a
voice test so Milo can say them aloud in simple Indian English, and write a
per-question rubric (expected points, misconceptions, two hints) for the
grader. Same LlamaIndex Workflow shape as gap_fill.py:

    start -> generate -> verify --(failed items, once)--> generate -> verify -> stop

- generate: one GENERATION_MODEL call per question, in parallel.
- verify: code checks first (sentence length, every number/unit kept, no
  banned Western/idiomatic words, rubric shape), then one VERIFY_MODEL call
  for the batch that answers each reworded question itself and compares it
  with the dataset answer, and checks the Bloom level/difficulty held.
- an item still failing after the retry keeps its ORIGINAL wording
  (reworded_text = original_text, spoken_text derived in code) and, if its
  last draft had a well-formed rubric, that rubric.

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


class PrepResult(BaseModel):
    questions: list[PreppedQuestion]


# ---- LLM output shapes ---------------------------------------------------
class _Rewording(BaseModel):
    reworded_text: str = Field(description="the question asked a new way, same meaning/answer/level")
    spoken_text: str = Field(description="reworded_text as Milo says it aloud: formulas and symbols in words")
    expected_points: list[str] = Field(description="2-4 points a full answer must contain")
    misconceptions: list[str] = Field(description="2-3 common wrong ideas students have on this question")
    hints: list[str] = Field(description="exactly 2: a gentle nudge, then a stronger one; never the answer")


class _Verdict(BaseModel):
    index: int
    answer_to_reworded: str = Field(description="your own short answer to the reworded question")
    ok: bool
    problem: str = Field(description="empty when ok; otherwise what is wrong, specifically")


class _Verdicts(BaseModel):
    verdicts: list[_Verdict]


GENERATE_SYSTEM = """You prepare a {board} Class {grade} {subject} question for a spoken voice test.
A friendly co-learner will read it aloud to an Indian school student, in a South Indian classroom.

Reword the question. Keep the SAME meaning, the SAME answer, the SAME Bloom level and difficulty.
Only the wording and the way of asking change. Ask for exactly what the original asks:
do not add or drop any part (no extra "give reason", no extra sub-question).
- Simple Indian English, as a teacher speaks in a South Indian classroom.
- Short sentences, about 15 words or fewer each. Everyday words.
- No idioms, no slang, no American or British phrasing.
- Keep textbook terms exactly as in NCERT/state-board books (for example potential difference, resistivity).
- Keep every number, unit and symbol from the original, written the same way.
- Only if the original already has a scenario, you may set it in a familiar Indian setting
  (ceiling fan, mixie, tube light, geyser, plug point, cricket, bus, rupees). Never add a scenario.
- Use phrasing students hear in class: "two times", "what will happen", "why is it like that".
- Never refer to a figure, diagram or table. Drop marks like "(2)" or "[3 marks]".

Also write:
- spoken_text: reworded_text exactly as it should be said aloud. Formulas and symbols in words
  ("V = IR" -> "V equals I times R", "2 Ω" -> "2 ohm", "m/s" -> "metre per second", "x²" -> "x squared"; chemical symbols as names: "Zn" -> "zinc").
- expected_points: 2 to 4 short points that a full answer must contain, taken from the given answer.
- misconceptions: 2 or 3 common wrong ideas students of this class have about this question.
- hints: exactly 2. First a gentle nudge, then a stronger one. A hint never states the answer
  or any part of it; for a multi-part question, not even one part's answer.
If `feedback` is given, your previous attempt was rejected for that reason - fix it."""

VERIFY_SYSTEM = """You are a strict {board} Class {grade} {subject} teacher checking reworded voice-test questions.
For each item: first answer the REWORDED question yourself, briefly, in `answer_to_reworded`.
Then ok=true only if ALL hold:
1. Your answer matches the dataset answer (same result, same key facts) - the meaning did not change.
2. The reworded question has the same Bloom level and difficulty as the original; it is not easier or harder.
3. It is clear, unambiguous and self-contained when heard aloud (no figure/diagram/table).
4. spoken_text says the same thing as reworded_text, with symbols and formulas in words.
5. It asks for exactly what the original asks - no part added or dropped.
6. expected_points agree with the dataset answer.
7. No hint states the answer or any part of it (for a multi-part question, any one part's answer counts).
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


def _has_word(text: str, word: str) -> bool:
    return re.search(rf"(?<![\w']){re.escape(word)}(?![\w'])", text, re.IGNORECASE) is not None


_MARKS = re.compile(r"[(\[]\s*\d+\s*(?:marks?)?\s*[)\]]\s*$", re.IGNORECASE)  # "(2)", "[3 marks]" at the end


def _problems(original: str, draft: _Rewording) -> str | None:
    """Everything that can be checked without a model; None when clean."""
    original = _MARKS.sub("", original.strip())
    text = draft.reworded_text.strip()
    if not text or not draft.spoken_text.strip():
        return "reworded_text or spoken_text is empty"
    long = [s for s in re.split(r"(?<=[.?!])\s+", text) if len(s.split()) > MAX_SENTENCE_WORDS]
    if long:
        return f"sentence longer than {MAX_SENTENCE_WORDS} words, split it: {long[0]!r}"
    missing = (set(_NUMBER.findall(original)) - set(_NUMBER.findall(text))) | (_units(original) - _units(text))
    if missing:
        return f"keep every number, unit and symbol from the original; missing: {', '.join(sorted(missing))}"
    banned = [w for w in BANNED_WORDS if _has_word(text, w) and not _has_word(original, w)]
    if banned:
        return f"use simple Indian classroom English; avoid: {', '.join(banned)}"
    return _rubric_problem(draft)


def _rubric_problem(draft: _Rewording) -> str | None:
    if not 2 <= len(_clean(draft.expected_points)) <= 4:
        return "expected_points must have 2 to 4 items"
    if not 2 <= len(_clean(draft.misconceptions)) <= 3:
        return "misconceptions must have 2 or 3 items"
    if len(_clean(draft.hints)) != 2:
        return "hints must have exactly 2 items"
    return None


def _clean(items: list[str]) -> list[str]:
    return [s.strip() for s in items if s.strip()]


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
                clean[i] = d
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
            problems = {v.index: v.problem for v in verdicts.verdicts if not v.ok}
            for i, d in clean.items():
                if i in ok:
                    accepted[i] = d
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
            logger.info("voice_prep_fallback_to_original", extra={"items": failed})
        return StopEvent(result=PrepResult(questions=[
            _result(i, c, accepted.get(i), last_draft.get(i)) for i, c in enumerate(picked)]))


def _result(i: int, c: Candidate, accepted: _Rewording | None, last: _Rewording | None) -> PreppedQuestion:
    rubric = accepted or (last if last and _rubric_problem(last) is None else None)
    return PreppedQuestion(
        order=i + 1, dataset_question_id=c.id, bloom_level=c.bloom_level, topic_id=c.topic_id,
        subtopic_id=c.subtopic_id, concepts=c.concepts, original_text=c.text,
        reworded_text=accepted.reworded_text.strip() if accepted else c.text,
        spoken_text=accepted.spoken_text.strip() if accepted else spoken(_MARKS.sub("", c.text.strip())),
        expected_points=_clean(rubric.expected_points) if rubric else [],
        misconceptions=_clean(rubric.misconceptions) if rubric else [],
        hints=_clean(rubric.hints) if rubric else [],
    )


async def prep_questions(llm: StructuredLLM, request: PrepRequest, *, generation_model: str, verify_model: str,
                         timeout: float = 170.0) -> PrepResult:
    workflow = VoicePrepWorkflow(llm, generation_model=generation_model, verify_model=verify_model, timeout=timeout)
    return await workflow.run(request=request)
