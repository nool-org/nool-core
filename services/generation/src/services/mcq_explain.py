"""Explanations and hints for MCQ growth-check questions that come from the
question bank (generated ones already carry their own, see gap_fill.py).

One batched call: for each question, a one-line "why" for the right option
(shown right after the student answers) and a nudge for the "Ask Milo"
lifeline that points at the idea without naming the answer. A hint that
gives the answer away comes back as null - core then uses its fallback.
"""

import json
import re

from pydantic import BaseModel, Field

from src.services.llm import StructuredLLM

MAX_ITEMS = 12

SYSTEM = """You help Class 6-12 students in India learn from multiple-choice questions.
For EACH question you get its text, its options and which option is correct. Write:
- explanation: ONE short sentence (under 25 words) saying WHY the correct option is right -
  the idea behind it, in simple Indian English. Don't just repeat the option.
- hint: ONE short sentence (under 20 words) that nudges the student toward the idea or the
  way to think about it, WITHOUT naming or quoting the correct option, and without ruling
  options in or out by letter.
Return one result per question, in the same order, with the same question_number."""


class McqItem(BaseModel):
    question_number: int = Field(ge=1)
    text: str = Field(min_length=1, max_length=2000)
    options: list[str] = Field(min_length=2, max_length=6)
    correct_index: int = Field(ge=0)
    concept: str = Field(default="", max_length=200)


class ExplainRequest(BaseModel):
    subject: str = Field(default="", max_length=100)
    items: list[McqItem] = Field(min_length=1, max_length=MAX_ITEMS)


class _Written(BaseModel):
    question_number: int
    explanation: str
    hint: str


class _Batch(BaseModel):
    results: list[_Written]


class Explained(BaseModel):
    question_number: int
    explanation: str | None
    hint: str | None


class ExplainResult(BaseModel):
    results: list[Explained]


def reveals(hint: str, answer: str) -> bool:
    """True when the hint names the correct option (whole words, any case)."""
    answer = answer.strip()
    return bool(answer) and re.search(rf"\b{re.escape(answer)}\b", hint, re.IGNORECASE) is not None


async def explain(llm: StructuredLLM, body: ExplainRequest, *, model: str) -> ExplainResult:
    payload = [{"question_number": i.question_number, "text": i.text, "options": i.options,
                "correct_option": i.options[i.correct_index] if i.correct_index < len(i.options) else "",
                "concept": i.concept} for i in body.items]
    batch = await llm(model=model, effort="low", system=SYSTEM,
                      user=f"Subject: {body.subject or 'general'}\nQUESTIONS:\n{json.dumps(payload)}",
                      output_type=_Batch)
    written = {w.question_number: w for w in batch.results}
    results = []
    for item in body.items:
        w = written.get(item.question_number)
        answer = item.options[item.correct_index] if item.correct_index < len(item.options) else ""
        explanation = (w.explanation.strip() or None) if w else None
        hint = (w.hint.strip() or None) if w else None
        if hint and reveals(hint, answer):
            hint = None
        results.append(Explained(question_number=item.question_number, explanation=explanation, hint=hint))
    return ExplainResult(results=results)
