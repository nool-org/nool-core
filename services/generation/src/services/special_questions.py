"""Writes the question types beyond MCQ - who_is_right, make_your_own,
fix_steps (core: services/question_types.py) - grounded in a topic's
textbook text. Any subject; core decides which slots to ask for.

Per slot: one GENERATION_MODEL call writes the question; who_is_right and
fix_steps then get an independent VERIFY_MODEL check ("is the marked answer
really right, and every other one really wrong?"); a failed one is rewritten
once with the problem. make_your_own is checked in core instead - its
example answer must pass its own code check (answer_checks.py). Returns only
the questions that passed; core fills the rest another way.
"""

import asyncio
import json
import logging
from typing import Literal

from pydantic import BaseModel, Field

from src.services.llm import StructuredLLM

logger = logging.getLogger(__name__)
MAX_CONTEXT = 12000
Kind = Literal["who_is_right", "make_your_own", "fix_steps"]

BASE = """You write one question for a CBSE Class {grade} {subject} student in India, on the topic "{topic}"
(chapter "{chapter}"), at Bloom level {level}. Use ONLY ideas from the textbook text given. Simple Indian
English, short sentences. Write maths as plain text with symbols: √5, 2 × 3, x², ≠, ≤, π - never LaTeX or $.
In Mathematics, build the question around a calculation or a worked problem (numbers to work with, a short
word problem) the way CBSE board papers do - not around a definition. Keep every line short: it is read on a
phone."""

PROMPTS = {
    "who_is_right": BASE + """
TYPE "Who is right?": two characters disagree about one claim from this topic, and the student decides.
- Zip is quick but jumps to conclusions; Bolt loves spotting patterns. Either may be the right one.
- question: the claim being debated, as a short yes/no question (e.g. "Can 6ⁿ end with the digit 0?").
- claims: exactly 2, Zip's then Bolt's - each one or two sentences, in their voice. One is right; the other
  makes a mistake real students make. Neither says "I am right".
- correct: 0 if Zip is right, 1 if Bolt is right.
- reasons: 3 or 4 short reasons for the right side; exactly one is the real reason (correct: true). The others
  sound plausible but are wrong or beside the point - give each a short snake_case mistake tag
  (e.g. "pattern_not_proof", "even_means_divisible").
- explanation: two sentences on why the right one is right.""",
    "fix_steps": BASE + """
TYPE "Fix Milo's homework": Milo, the class owl, solved a problem and made ONE mistake.
- problem: the problem Milo solved, one or two sentences.
- steps: Milo's worked solution, 3 to 6 steps, each one line. Exactly one step is wrong (a real student
  mistake - e.g. taking the highest power for the HCF); every later step follows from it honestly.
- wrong_step: index (from 0) of the wrong step.
- fixes: 3 or 4 versions of that step; exactly one is correct (correct: true); the others are other common
  mistakes, each with a snake_case mistake tag.
- explanation: one or two sentences on what went wrong and why the fix is right.""",
    "make_your_own": BASE + """
TYPE "Make your own": the student builds something that fits a condition - many answers are right and
code checks the student's. Pick ONE check from this list and give its params as a JSON object:
{checks}
- question: the instruction, naming the condition exactly (e.g. "Give two numbers whose HCF is 6 and LCM
  is 36."). Don't hint at an answer.
- check_kind and params_json: the check and its params - they must match the question exactly.
- labels: one short label per answer box (e.g. ["First number", "Second number"]).
- example: one correct answer, one value per box, as strings.
- explanation: one sentence on how to build an answer.""",
}

VERIFY = """You check one {kind} question for a Class {grade} {subject} student. Work it out yourself first.
Is the marked answer really correct, and is every other choice really wrong (or, for reasons/fixes,
really not the right one)? Is it clear and on the topic? Answer ok=true only if all of that holds;
otherwise ok=false and say what's wrong in problem."""


class Claim(BaseModel):
    name: str
    says: str


class Choice(BaseModel):
    text: str
    correct: bool
    mistake: str  # snake_case tag for a wrong one, "" for the right one


class WhoIsRight(BaseModel):
    question: str
    claims: list[Claim]
    correct: int
    reasons: list[Choice]
    explanation: str


class FixSteps(BaseModel):
    problem: str
    steps: list[str]
    wrong_step: int
    fixes: list[Choice]
    explanation: str


class MakeYourOwn(BaseModel):
    question: str
    check_kind: str
    params_json: str
    labels: list[str]
    example: list[str]
    explanation: str


class Verdict(BaseModel):
    ok: bool
    problem: str


class SpecialSlot(BaseModel):
    kind: Kind
    bloom_level: str


class CheckInfo(BaseModel):
    kind: str
    describe: str


class SpecialRequest(BaseModel):
    grade: int = Field(ge=1, le=12)
    subject: str = Field(max_length=200)
    chapter: str = Field(max_length=300)
    topic: str = Field(min_length=1, max_length=300)
    context: str = Field(min_length=1, max_length=MAX_CONTEXT * 4)
    slots: list[SpecialSlot] = Field(min_length=1, max_length=6)
    checks: list[CheckInfo] = Field(default_factory=list)
    avoid: list[str] = Field(default_factory=list, max_length=30)  # questions already in the pool


class SpecialQuestion(BaseModel):
    slot_index: int
    kind: Kind
    bloom_level: str
    text: str
    spec: dict


class SpecialResult(BaseModel):
    questions: list[SpecialQuestion]


OUTPUT = {"who_is_right": WhoIsRight, "fix_steps": FixSteps, "make_your_own": MakeYourOwn}


def to_spec(kind: str, draft) -> tuple[str, dict]:
    """(question text, core's spec) from a draft."""
    if kind == "who_is_right":
        names = ["Zip", "Bolt"]  # the app's cast, whatever the model called them
        return draft.question, {"claims": [{"name": names[i], "says": c.says} for i, c in enumerate(draft.claims[:2])],
                                "correct": draft.correct, "explanation": draft.explanation,
                                "reasons": [r.model_dump() for r in draft.reasons]}
    if kind == "fix_steps":
        return draft.problem, {"problem": draft.problem, "steps": draft.steps, "wrong_step": draft.wrong_step,
                               "fixes": [f.model_dump() for f in draft.fixes], "explanation": draft.explanation}
    try:
        params = json.loads(draft.params_json)
    except json.JSONDecodeError:
        params = {}
    return draft.question, {"check": {"kind": draft.check_kind, "params": params}, "labels": draft.labels,
                            "example": draft.example, "explanation": draft.explanation}


async def _one(llm: StructuredLLM, body: SpecialRequest, i: int, slot: SpecialSlot, *, model: str,
               verify_model: str) -> SpecialQuestion | None:
    checks = "\n".join(f'- {c.kind}: {c.describe}' for c in body.checks) or "(none)"
    system = PROMPTS[slot.kind].format(grade=body.grade, subject=body.subject, topic=body.topic,
                                       chapter=body.chapter, level=slot.bloom_level, checks=checks)
    user = f"TEXTBOOK TEXT:\n{body.context[:MAX_CONTEXT]}"
    if body.avoid:
        user += "\n\nDon't repeat these questions:\n- " + "\n- ".join(body.avoid)
    for attempt in range(2):
        try:
            draft = await llm(model=model, effort="low", system=system, user=user, output_type=OUTPUT[slot.kind])
            text, spec = to_spec(slot.kind, draft)
            if slot.kind != "make_your_own":
                verdict = await llm(model=verify_model, effort="low",
                                    system=VERIFY.format(kind=slot.kind, grade=body.grade, subject=body.subject),
                                    user=json.dumps({"question": text, **spec}, ensure_ascii=False),
                                    output_type=Verdict)
                if not verdict.ok:
                    user += f"\n\nYour last question had this problem - fix it:\n{verdict.problem}"
                    continue
            return SpecialQuestion(slot_index=i, kind=slot.kind, bloom_level=slot.bloom_level, text=text, spec=spec)
        except Exception as exc:  # one slot's failure mustn't sink the others
            logger.warning("special_question_failed", extra={"kind": slot.kind, "attempt": attempt,
                                                             "error": type(exc).__name__})
    return None


async def write_questions(llm: StructuredLLM, body: SpecialRequest, *, model: str, verify_model: str) -> SpecialResult:
    made = await asyncio.gather(*(_one(llm, body, i, s, model=model, verify_model=verify_model)
                                  for i, s in enumerate(body.slots)))
    return SpecialResult(questions=[q for q in made if q is not None])
