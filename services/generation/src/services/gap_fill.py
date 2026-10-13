"""Gap filling: write questions for the homework slots the question bank
couldn't fill (see core's services/question_selector.py), grounded in the
textbook section the student is weak on. A LlamaIndex Workflow:

    start -> generate -> verify --(failed slots, once)--> generate -> verify -> stop

- generate: one LLM call per open slot, all in parallel (wall time ~ one
  question, not N), from the section's textbook text, with bank questions
  as style examples.
- verify: a second, independent call checks each draft (correct answer,
  grounded in the text, matches Bloom level/difficulty, self-contained,
  not a copy). Failed slots go back to generate once with the verifier's
  reason as feedback; slots still failing come back as `unfilled` rather
  than as a wrong question.

Stateless: everything it needs arrives in the request (core owns the DB,
access rules and what the student has already seen).
"""

import asyncio
import json
import logging
import re
from typing import Literal

from llama_index.core.workflow import Context, Event, StartEvent, StopEvent, Workflow, step
from pydantic import BaseModel, Field

from src.services.llm import StructuredLLM

logger = logging.getLogger(__name__)

BloomLevel = Literal["REMEMBER", "UNDERSTAND", "APPLY", "ANALYZE", "EVALUATE", "CREATE"]
Difficulty = Literal["EASY", "MEDIUM", "HARD"]


# ---- request / result ----------------------------------------------------
class Slot(BaseModel):
    bloom_level: BloomLevel
    difficulty: Difficulty
    # set by core's MCQ test: an mcq on this concept, wrong options built from these misconceptions
    question_type: Literal["mcq"] | None = None
    concept: str | None = None
    misconceptions: list[str] = Field(default_factory=list)


class Section(BaseModel):
    title: str
    text: str


class Example(BaseModel):
    text: str
    answer: str
    bloom_level: BloomLevel | None = None
    difficulty: Difficulty | None = None


class GapFillRequest(BaseModel):
    subject: str
    grade: int
    chapter: str
    sections: list[Section] = Field(min_length=1, description="the weak section(s)' textbook text")
    slots: list[Slot] = Field(min_length=1, max_length=30)  # each slot = one parallel LLM call
    examples: list[Example] = Field(default_factory=list, description="bank questions to match in style, never to copy")
    avoid: list[str] = Field(default_factory=list, description="question texts already in this homework")
    # "spoken": the question is read aloud in a voice test (core's voice-test prep), so it must be short
    style: Literal["written", "spoken"] = "written"


SPOKEN_RULES = """
- These questions are ASKED ALOUD in a voice test and answered by speaking: one single question, at most
  35 words, no sub-parts, no lists, no dialogue or story set-up, nothing to write, draw or calculate at length.
  An EVALUATE question asks the student to judge or justify in one or two spoken sentences."""


class Question(BaseModel):
    slot_index: int
    question_type: Literal["short_answer", "long_answer", "numerical", "mcq"]
    text: str
    options: list[str] = Field(description="exactly 4 options for mcq, otherwise empty")
    answer: str = Field(description="the model answer a student is marked against")
    solution: str = Field(description="worked solution / explanation")
    # mcq only (core's MCQ check game), empty otherwise
    explanation: str = Field(description="mcq: one short line on why the right option is right; else empty")
    hint: str = Field(description="mcq: a one-line nudge toward the idea, never naming or giving the answer; else empty")
    grounded_in: str = Field(description="title of the section the question is based on")


class GapFillResult(BaseModel):
    questions: list[Question]
    unfilled_slot_indexes: list[int]
    attempts: int


# ---- LLM output shapes ---------------------------------------------------
class _Drafts(BaseModel):
    questions: list[Question]


class _Verdict(BaseModel):
    slot_index: int
    ok: bool
    problem: str = Field(description="empty when ok; otherwise what is wrong, specifically")


class _Verdicts(BaseModel):
    verdicts: list[_Verdict]


GENERATE_SYSTEM = """You write homework questions for CBSE Class {grade} {subject} students.
Write exactly one question for each requested slot, based ONLY on the given NCERT textbook sections -
no facts, formulas or data that aren't in them or standard for this class.
- Match each slot's Bloom level (REMEMBER: recall; UNDERSTAND: explain; APPLY: use in a new situation or
  a numerical; ANALYZE: compare/break down; EVALUATE: justify/judge; CREATE: design/propose) and difficulty.
- Text only: never refer to a figure, diagram or table the student can't see.
- The examples show the style and level of the question bank - match them, never copy or paraphrase them,
  and don't repeat anything in `avoid`.
- Give a model answer a teacher can mark against, and a short worked solution.
- A slot with question_type "mcq" must be an mcq on its `concept` with exactly 4 options; build the wrong
  options from its `misconceptions` (each a plausible belief a student really holds), never silly ones.
  Give it an `explanation` (one short line: why the right option is right) and a `hint` (one line nudging
  toward the idea - it never names, quotes or gives away the right option).
- If a slot has `feedback`, your previous question for it was rejected for that reason - fix it."""

# Extra rules per subject, appended to GENERATE_SYSTEM. Maths is tested on problems, the way CBSE board
# papers do it - not on statements about the topic.
SUBJECT_STYLE = {
    "Mathematics": """
MATHS - write PROBLEMS, not text questions:
- Almost every question is something to work out: numbers to compute, a short word problem, a value to find,
  a claim to check by calculating - the way CBSE Class 10 board papers ask (e.g. find the HCF/LCM of given
  numbers; a real-life word problem such as bells or lights repeating, packing or cutting into equal parts;
  find the other number given HCF, LCM and one number; find the smallest n that makes a product a perfect
  square; decide whether a given number can end in a digit). Only a REMEMBER slot may ask for a statement or
  definition, and even then prefer recognising a correct example.
- Use numbers a Class 10 student can handle on paper in 1-3 minutes, no calculator.
- MCQ options are answers (numbers or short expressions). Every wrong option is what a typical mistake
  gives - LCM instead of HCF, highest powers instead of lowest, a dropped prime, an arithmetic slip - never a
  random number.
- Write maths as plain text with symbols: 2³ × 3², √5, ≠, ≤ - never LaTeX or $.""",
}

VERIFY_SYSTEM = """You are a strict CBSE Class {grade} {subject} teacher checking generated homework questions.
For each question, ok=true only if ALL hold:
1. The answer is correct and complete (re-derive any numericals yourself - work every calculation out).
2. It is answerable from the given textbook sections at Class {grade} level.
3. It fits its requested Bloom level and difficulty reasonably.
4. It is self-contained (no unseen figure/diagram/table) and unambiguous; an mcq has exactly one correct option,
   a correct one-line explanation, and a hint that never gives the right option away.
5. It is not a copy or near-copy of an example, an `avoid` item, or another question in this batch
   (when two in the batch overlap, reject the later slot).
If not ok, say precisely what is wrong in `problem`."""


def reveals(hint: str, answer: str) -> bool:
    """The hint contains the right option's text (as whole words)."""
    return re.search(rf"(?<!\w){re.escape(answer.strip())}(?!\w)", hint, re.IGNORECASE) is not None


def _malformed(q: Question, slot: Slot) -> str | None:
    if slot.question_type == "mcq" and q.question_type != "mcq":
        return "this slot needs an mcq with exactly 4 options"
    if not q.text.strip() or not q.answer.strip():
        return "the question or its answer is empty"
    if q.question_type == "mcq":
        options = [o.strip() for o in q.options]
        if len(options) != 4 or not all(options) or len(set(options)) != 4:
            return "an mcq needs exactly 4 distinct, non-empty options"
        if q.answer.strip() not in options:
            return "the mcq answer must be one of its options, word for word"
        if slot.question_type == "mcq" and not q.explanation.strip():
            return "an mcq needs a one-line explanation of why the answer is right"
        if slot.question_type == "mcq" and (not q.hint.strip() or reveals(q.hint, q.answer)):
            return "an mcq needs a hint that never contains the right option"
    elif q.options:
        return "only an mcq has options"
    return None


# ---- workflow ------------------------------------------------------------
class GenerateEvent(Event):
    slot_indexes: list[int]
    attempt: int
    feedback: dict[int, str]


class VerifyEvent(Event):
    slot_indexes: list[int]
    attempt: int
    drafts: list[Question]


class GapFillWorkflow(Workflow):
    def __init__(self, llm: StructuredLLM, *, generation_model: str, verify_model: str,
                 generation_effort: str = "low", verify_effort: str = "low", max_attempts: int = 2, **kwargs):
        super().__init__(**kwargs)
        self.llm = llm
        self.generation_model, self.verify_model = generation_model, verify_model
        self.generation_effort, self.verify_effort = generation_effort, verify_effort
        self.max_attempts = max_attempts

    @step
    async def start(self, ctx: Context, ev: StartEvent) -> GenerateEvent:
        request: GapFillRequest = ev.request
        await ctx.store.set("request", request)
        await ctx.store.set("accepted", {})
        return GenerateEvent(slot_indexes=list(range(len(request.slots))), attempt=1, feedback={})

    @step
    async def generate(self, ctx: Context, ev: GenerateEvent) -> VerifyEvent:
        request: GapFillRequest = await ctx.store.get("request")
        system = GENERATE_SYSTEM.format(grade=request.grade, subject=request.subject) + SUBJECT_STYLE.get(
            request.subject, "")
        if request.style == "spoken":
            system += SPOKEN_RULES
        # a retry must not repeat what an earlier attempt already got accepted
        avoid = request.avoid + [q.text for q in (await ctx.store.get("accepted")).values()]

        async def one(i: int) -> Question | None:
            payload = {
                "chapter": request.chapter,
                "sections": [s.model_dump() for s in request.sections],
                "slots": [{"slot_index": i, **request.slots[i].model_dump(exclude_defaults=True),
                           **({"feedback": ev.feedback[i]} if i in ev.feedback else {})}],
                "other_slots_in_this_homework": [request.slots[j].model_dump(exclude_defaults=True) for j in range(len(request.slots)) if j != i],
                "examples": [e.model_dump(exclude_none=True) for e in request.examples],
                "avoid": avoid,
            }
            try:
                drafts = await self.llm(model=self.generation_model, effort=self.generation_effort, system=system,
                                        user=json.dumps(payload, ensure_ascii=False), output_type=_Drafts)
            except Exception as exc:  # one slot's failure mustn't sink the others; verify marks it unfilled
                logger.warning("gap_fill_generate_failed", extra={"slot": i, "error": type(exc).__name__})
                return None
            q = next(iter(drafts.questions), None)
            return q.model_copy(update={"slot_index": i}) if q else None

        results = await asyncio.gather(*(one(i) for i in ev.slot_indexes))
        return VerifyEvent(slot_indexes=ev.slot_indexes, attempt=ev.attempt, drafts=[q for q in results if q])

    @step
    async def verify(self, ctx: Context, ev: VerifyEvent) -> GenerateEvent | StopEvent:
        request: GapFillRequest = await ctx.store.get("request")
        accepted: dict[int, Question] = await ctx.store.get("accepted")
        feedback: dict[int, str] = {}
        drafts = []
        for q in ev.drafts:  # shape checks first - no LLM needed to reject these
            problem = _malformed(q, request.slots[q.slot_index])
            if problem:
                feedback[q.slot_index] = problem
            else:
                drafts.append(q.model_copy(update={"options": [o.strip() for o in q.options]}))
        if drafts:
            payload = {
                "sections": [s.model_dump() for s in request.sections],
                "examples": [e.text for e in request.examples],
                "avoid": request.avoid + [q.text for q in accepted.values()],
                "questions": [{**q.model_dump(), "requested": request.slots[q.slot_index].model_dump(exclude_defaults=True)} for q in drafts],
            }
            try:
                verdicts = await self.llm(
                    model=self.verify_model, effort=self.verify_effort,
                    system=VERIFY_SYSTEM.format(grade=request.grade, subject=request.subject),
                    user=json.dumps(payload, ensure_ascii=False), output_type=_Verdicts,
                )
            except Exception as exc:  # keep what's already accepted; these drafts count as unverified
                logger.warning("gap_fill_verify_failed", extra={"slots": len(drafts), "error": type(exc).__name__})
                verdicts = _Verdicts(verdicts=[])
            ok = {v.slot_index for v in verdicts.verdicts if v.ok}
            problems = {v.slot_index: v.problem for v in verdicts.verdicts if not v.ok}
            for q in drafts:
                if q.slot_index in ok:
                    accepted[q.slot_index] = q
                else:
                    feedback[q.slot_index] = problems.get(q.slot_index) or "not verified"
        failed = [i for i in ev.slot_indexes if i not in accepted]
        for i in failed:
            feedback.setdefault(i, "no question was produced for this slot")
        await ctx.store.set("accepted", accepted)

        if failed and ev.attempt < self.max_attempts:
            return GenerateEvent(slot_indexes=failed, attempt=ev.attempt + 1, feedback=feedback)
        return StopEvent(result=GapFillResult(
            questions=[accepted[i] for i in sorted(accepted)], unfilled_slot_indexes=failed, attempts=ev.attempt,
        ))


async def fill_gaps(llm: StructuredLLM, request: GapFillRequest, *, generation_model: str, verify_model: str,
                    timeout: float = 170.0) -> GapFillResult:
    workflow = GapFillWorkflow(llm, generation_model=generation_model, verify_model=verify_model, timeout=timeout)
    return await workflow.run(request=request)
