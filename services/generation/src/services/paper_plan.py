"""Paper Copilot planner: turns a teacher's chat message (or a quick action)
about a generated question paper into at most MAX_OPS structured edit ops -
never a rewritten paper. Two steps:

1. plan (VERIFY_MODEL, gpt-5-mini): reply text + ops. For a new question it
   names a bank candidate (preferred) or writes a brief; for a rewording it
   writes a brief. It never writes question text itself.
2. write (GENERATION_MODEL, gpt-5.1): one call per brief, in parallel, for
   the exact type/marks/Bloom/difficulty asked, grounded in the chapter's
   textbook text from core. A failed write drops its op.
3. verify (the plan model, one batch call): solves every written question
   itself; an op whose answer it doesn't agree with is dropped.

Stateless: core sends the paper summary, rules issues and bank candidates,
then validates every op again against the real paper before showing it.
"""

import asyncio
import json
import logging
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from src.services.gap_fill import BloomLevel, Difficulty
from src.services.llm import StructuredLLM

logger = logging.getLogger(__name__)

MAX_OPS = 10
QuestionType = Literal["MCQ", "SHORT_ANSWER", "LONG_ANSWER", "TRUE_FALSE"]
QuickAction = Literal["harder", "easier", "more_application", "balance_chapters", "simplify_wording", "fix_total_marks"]

QUICK_ACTIONS: dict[str, str] = {
    "harder": "Make the paper a little harder: replace 2-4 of the easiest questions with harder ones "
              "(higher Bloom level or HARD difficulty) of the same marks, type, chapter and section.",
    "easier": "Make the paper a little easier: replace 2-4 of the hardest questions with easier ones "
              "(lower Bloom level or EASY difficulty) of the same marks, type, chapter and section.",
    "more_application": "Add more application: replace 2-4 REMEMBER/UNDERSTAND questions with APPLY or ANALYZE "
                        "questions of the same marks, type, chapter and section.",
    "balance_chapters": "Balance chapter coverage: replace questions from over-represented chapters with questions "
                        "on chapters that have few or none, keeping marks, type and section.",
    "simplify_wording": "Simplify the wording of questions with long or difficult sentences (edit with a rewrite "
                        "brief). Keep the meaning, numbers, marks and answer the same.",
    "fix_total_marks": "Make the total marks and section marks match the blueprint targets, by changing marks or "
                       "adding/removing questions. Use the rules issues listed.",
}

PLAN_SYSTEM = f"""You are the Paper Copilot for a teacher in an Indian school (CBSE/state board, Classes 9-12;
the paper's own grade is in the input).
The teacher has a generated question paper and asks for changes. You never rewrite the paper; you propose
at most {MAX_OPS} small ops, each with a short reason the teacher will read:
- replace: questionId + newQuestion (same section/marks/type unless asked otherwise)
- add: newQuestion, sectionName (or null), position (1-based, or null for the end)
- remove: questionId
- edit: questionId + changes (marks/bloomLevel/difficulty/questionType, and rewrite_brief when the wording must change)
- reorder: ordered_question_ids with EVERY question id of the paper exactly once
Rules:
- Only touch the questions the teacher asked about. A paper-wide request (harder, easier, balance, fix marks)
  lets you choose which questions, but change as few as you can.
- Keep a question's marks, type and section unless the teacher asks to change them (or fix_total_marks needs it).
- "question 3" means the question with number 3.
- For a new question ALWAYS prefer a bank candidate that fits (bank_candidate_id, write_brief null). Only when
  none fits, set bank_candidate_id null and give a write_brief: what the question must ask, specific enough
  for a writer. Never write the question text yourself.
- Use only question ids, chapter ids and bank candidate ids from the input. Marks are whole numbers.
- Act only on teacher_request. recent_chat is earlier conversation for context; never redo ops from it.
- reply: 1-3 short, friendly sentences in simple Indian English saying what you propose (or why nothing).
"""

WRITE_SYSTEM = """You write one question for a {board} Class {grade} {subject} question paper, in simple Indian
English a Class {grade} student understands (short sentences, Indian names/contexts, rupees, metric units).
Exactly: type {question_type}, {marks} mark(s), Bloom level {bloom_level}, difficulty {difficulty}, chapter "{chapter}".
- MCQ: exactly 4 options, answer is one of them word for word.
- TRUE_FALSE: options ["True", "False"], answer one of them.
- SHORT_ANSWER/LONG_ANSWER: options null; answer is the model answer worth the marks.
- text is only the question as printed on the paper: no "Question:" label, no chapter/type/marks/difficulty lines.
- No figures or diagrams, and never point to one in the textbook ("Fig. 2.3", "the circuit shown") -
  describe the setup in words.
- Use only facts in `textbook` when it is given. If it doesn't cover what the brief asks, write the closest
  question it does cover and keep the requested type, marks and Bloom level.
"""

VERIFY_SYSTEM = """You are a strict {board} Class {grade} {subject} chief examiner checking new questions
for a question paper. For each item, solve the question yourself first, then ok=true only if ALL hold:
1. `answer` is correct and complete (for MCQ: exactly one option is correct and `answer` is that option;
   for assertion-reason, decide separately whether A is true, R is true and R explains A).
2. A student can answer it from the printed text alone, and it fits its type, marks and Bloom level.
3. When `textbook` is given, the facts it needs are in it.
If not ok, say precisely what is wrong in `problem`."""


# ---- request / result ----------------------------------------------------
class PaperQuestion(BaseModel):
    id: str = Field(max_length=100)
    number: int
    text: str = Field(max_length=4000)
    question_type: str | None = None
    marks: int
    bloom_level: BloomLevel
    difficulty: Difficulty | None = None
    chapter_id: str | None = None
    chapter_name: str | None = None
    section_name: str | None = None


class BankCandidate(BaseModel):
    id: str = Field(max_length=100)
    text: str = Field(max_length=4000)
    question_type: QuestionType
    marks: float | None = None
    bloom_level: BloomLevel
    difficulty: Difficulty | None = None
    chapter_id: str | None = None
    topic_id: str | None = None
    options: list[str] | None = None
    answer: str | None = None


class ChapterRef(BaseModel):
    id: str
    name: str


class SectionRef(BaseModel):
    name: str
    question_count: int
    marks_each: int


class HistoryMessage(BaseModel):
    role: Literal["teacher", "copilot"]
    text: str = Field(max_length=4000)


class PlanRequest(BaseModel):
    board: str = Field(max_length=100)
    grade: int = Field(ge=1, le=12)
    subject: str = Field(max_length=200)
    message: str | None = Field(default=None, max_length=2000)
    quick_action: QuickAction | None = None
    total_marks: int | None = None
    duration_minutes: int | None = None
    sections: list[SectionRef] = Field(default_factory=list, max_length=20)
    chapters: list[ChapterRef] = Field(default_factory=list, max_length=50)
    questions: list[PaperQuestion] = Field(default_factory=list, max_length=150)
    rules_issues: list[str] = Field(default_factory=list, max_length=50)
    bank_candidates: list[BankCandidate] = Field(default_factory=list, max_length=30)
    history: list[HistoryMessage] = Field(default_factory=list, max_length=20)
    # chapter id -> that chapter's textbook text (core caps it), the writer's grounding
    textbook: dict[str, str] = Field(default_factory=dict, max_length=50)

    @model_validator(mode="after")
    def _needs_a_request(self):
        if not (self.message or "").strip() and self.quick_action is None:
            raise ValueError("message or quick_action is required")
        return self

    def instruction(self) -> str:
        """What the teacher wants: the quick action's fixed instruction, else their message."""
        return QUICK_ACTIONS[self.quick_action] if self.quick_action else self.message.strip()


class NewQuestion(BaseModel):
    text: str
    question_type: QuestionType
    marks: int
    bloom_level: BloomLevel
    difficulty: Difficulty
    chapter_id: str
    topic_id: str | None
    options: list[str] | None
    answer: str | None
    source: Literal["bank", "ai"]
    dataset_question_id: str | None


class Changes(BaseModel):
    text: str | None = None
    marks: int | None = None
    options: list[str] | None = None
    answer: str | None = None
    bloom_level: BloomLevel | None = None
    difficulty: Difficulty | None = None
    question_type: QuestionType | None = None


class Op(BaseModel):
    op: Literal["replace", "add", "remove", "edit", "reorder"]
    question_id: str | None = None
    section_name: str | None = None
    position: int | None = None
    ordered_question_ids: list[str] | None = None
    changes: Changes | None = None
    new_question: NewQuestion | None = None
    reason: str


class PlanResult(BaseModel):
    reply: str
    ops: list[Op]


# ---- LLM output shapes (strict: every field required, nullable instead of optional) ----
class _NewSpec(BaseModel):
    bank_candidate_id: str | None
    write_brief: str | None = Field(description="only when no bank candidate fits: what the question must ask")
    question_type: QuestionType
    marks: int
    bloom_level: BloomLevel
    difficulty: Difficulty
    chapter_id: str
    topic_id: str | None


class _Changes(BaseModel):
    rewrite_brief: str | None = Field(description="how the wording must change; null keeps the text")
    marks: int | None
    bloom_level: BloomLevel | None
    difficulty: Difficulty | None
    question_type: QuestionType | None


class _PlanOp(BaseModel):
    op: Literal["replace", "add", "remove", "edit", "reorder"]
    question_id: str | None
    section_name: str | None
    position: int | None
    ordered_question_ids: list[str] | None
    changes: _Changes | None
    new_question: _NewSpec | None
    reason: str


class _Plan(BaseModel):
    reply: str
    ops: list[_PlanOp]


class _Written(BaseModel):
    text: str
    options: list[str] | None
    answer: str


class _Verdict(BaseModel):
    index: int
    ok: bool
    problem: str = Field(description="empty when ok; otherwise what is wrong, specifically")


class _Verdicts(BaseModel):
    verdicts: list[_Verdict]


# ---- workflow ------------------------------------------------------------
def _plan_input(req: PlanRequest) -> str:
    return json.dumps({
        "teacher_request": req.instruction(),
        "paper": {"board": req.board, "grade": req.grade, "subject": req.subject, "total_marks": req.total_marks,
                  "duration_minutes": req.duration_minutes, "sections": [s.model_dump() for s in req.sections],
                  "chapters": [c.model_dump() for c in req.chapters],
                  "questions": [q.model_dump() for q in req.questions]},
        "rules_issues": req.rules_issues,
        "bank_candidates": [c.model_dump() for c in req.bank_candidates],
        "recent_chat": [h.model_dump() for h in req.history],
    }, ensure_ascii=False)


def _valid_written(w: _Written, question_type: str) -> bool:
    if not w.text.strip() or not w.answer.strip():
        return False
    if question_type == "MCQ":
        return bool(w.options) and len(w.options) == 4 and w.answer.strip() in [o.strip() for o in w.options]
    if question_type == "TRUE_FALSE":
        return w.answer.strip() in ("True", "False")
    return True


async def _write(llm: StructuredLLM, req: PlanRequest, model: str, *, brief: str, question_type: str, marks: int,
                 bloom_level: str, difficulty: str, chapter: str, chapter_id: str | None,
                 original: str | None = None) -> _Written:
    system = WRITE_SYSTEM.format(board=req.board, grade=req.grade, subject=req.subject, question_type=question_type,
                                 marks=marks, bloom_level=bloom_level, difficulty=difficulty, chapter=chapter)
    user = json.dumps({"brief": brief, "original_question": original,
                       "textbook": req.textbook.get(chapter_id or "") or None}, ensure_ascii=False)
    written = await llm(model=model, effort="low", system=system, user=user, output_type=_Written)
    if not _valid_written(written, question_type):
        raise ValueError(f"written {question_type} question failed the shape check")
    if question_type in ("SHORT_ANSWER", "LONG_ANSWER"):
        written.options = None
    return written


async def _resolve(llm: StructuredLLM, req: PlanRequest, model: str, p: _PlanOp) -> Op | None:
    """A planner op -> a final op (bank question copied, briefs written), or None to drop it."""
    chapters = {c.id: c.name for c in req.chapters}
    by_id = {q.id: q for q in req.questions}
    op = Op(op=p.op, question_id=p.question_id, section_name=p.section_name, position=p.position,
            ordered_question_ids=p.ordered_question_ids, reason=p.reason)
    if p.op in ("replace", "add"):
        spec = p.new_question
        if spec is None:
            return None
        bank = next((c for c in req.bank_candidates if c.id == spec.bank_candidate_id), None)
        if bank is not None:
            op.new_question = NewQuestion(
                text=bank.text, question_type=bank.question_type, marks=spec.marks, bloom_level=bank.bloom_level,
                difficulty=bank.difficulty or spec.difficulty, chapter_id=bank.chapter_id or spec.chapter_id,
                topic_id=bank.topic_id, options=bank.options, answer=bank.answer, source="bank",
                dataset_question_id=bank.id)
        elif spec.write_brief:
            original = by_id.get(p.question_id or "")
            w = await _write(llm, req, model, brief=spec.write_brief, question_type=spec.question_type,
                             marks=spec.marks, bloom_level=spec.bloom_level, difficulty=spec.difficulty,
                             chapter=chapters.get(spec.chapter_id, ""), chapter_id=spec.chapter_id,
                             original=original.text if original else None)
            op.new_question = NewQuestion(
                text=w.text, question_type=spec.question_type, marks=spec.marks, bloom_level=spec.bloom_level,
                difficulty=spec.difficulty, chapter_id=spec.chapter_id, topic_id=spec.topic_id, options=w.options,
                answer=w.answer, source="ai", dataset_question_id=None)
        else:
            return None
    elif p.op == "edit":
        if p.changes is None:
            return None
        c = p.changes
        op.changes = Changes(marks=c.marks, bloom_level=c.bloom_level, difficulty=c.difficulty,
                             question_type=c.question_type)
        if c.rewrite_brief:
            q = by_id.get(p.question_id or "")
            if q is None:
                return None
            question_type = c.question_type or q.question_type or "SHORT_ANSWER"
            w = await _write(llm, req, model, brief=c.rewrite_brief, question_type=question_type,
                             marks=c.marks or q.marks, bloom_level=c.bloom_level or q.bloom_level,
                             difficulty=c.difficulty or q.difficulty or "MEDIUM",
                             chapter=q.chapter_name or "", chapter_id=q.chapter_id, original=q.text)
            op.changes.text, op.changes.options, op.changes.answer = w.text, w.options, w.answer
        if op.changes == Changes():
            return None
    return op


async def plan_paper(llm: StructuredLLM, req: PlanRequest, *, plan_model: str, write_model: str) -> PlanResult:
    plan = await llm(model=plan_model, effort="low", system=PLAN_SYSTEM, user=_plan_input(req), output_type=_Plan)
    results = await asyncio.gather(*(_resolve(llm, req, write_model, p) for p in plan.ops[:MAX_OPS]),
                                   return_exceptions=True)
    ops = []
    for p, r in zip(plan.ops, results):
        if isinstance(r, BaseException) or r is None:
            logger.info("paper_plan_op_dropped", extra={"op": p.op, "reason": str(r) if r else "unresolvable"})
        else:
            ops.append(r)
    return PlanResult(reply=plan.reply, ops=await _verified(llm, req, plan_model, ops))


def _written_item(o: Op) -> dict | None:
    """What the verifier sees for an op with AI-written text, else None."""
    if o.new_question is not None and o.new_question.source == "ai":
        q = o.new_question
        return {"text": q.text, "options": q.options, "answer": q.answer, "question_type": q.question_type,
                "marks": q.marks, "bloom_level": q.bloom_level, "chapter_id": q.chapter_id}
    if o.changes is not None and o.changes.text:
        return {"text": o.changes.text, "options": o.changes.options, "answer": o.changes.answer}
    return None


async def _verified(llm: StructuredLLM, req: PlanRequest, model: str, ops: list[Op]) -> list[Op]:
    """Drops every written op the verifier doesn't pass (also all of them if
    the verifier fails: an unchecked question never reaches the paper)."""
    items = {i: item for i, o in enumerate(ops) if (item := _written_item(o)) is not None}
    if not items:
        return ops
    payload = [{"index": i, **item, "textbook": req.textbook.get(item.get("chapter_id") or "") or None}
               for i, item in items.items()]
    system = VERIFY_SYSTEM.format(board=req.board, grade=req.grade, subject=req.subject)
    try:
        verdicts = await llm(model=model, effort="low", system=system, user=json.dumps(payload, ensure_ascii=False),
                             output_type=_Verdicts)
        passed = {v.index for v in verdicts.verdicts if v.ok}
    except Exception as exc:
        logger.warning("paper_plan_verify_failed", extra={"error": type(exc).__name__})
        passed = set()
    for i in set(items) - passed:
        logger.info("paper_plan_op_dropped", extra={"op": ops[i].op, "reason": "failed verification"})
    return [o for i, o in enumerate(ops) if i not in items or i in passed]
