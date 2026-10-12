"""Topic quiz analysis: after a student's MCQ quiz on one topic, a short
read of how they did - which Bloom level is lagging and what that means
here, the 2-3 things they still need to know (from the questions they
missed) and one next step. Core works out the per-level scores and the
lagging level in code (services/core/src/services/topic_quiz.py); this only
writes the words, so a failure there falls back to plain text in core.

One VERIFY_MODEL (gpt-5-mini, low effort) call.
"""

from pydantic import BaseModel, Field

from src.services.llm import StructuredLLM

SYSTEM = """You are a kind, direct teacher reading a CBSE Class {grade} {subject} student's MCQ quiz on
the topic "{topic}" (chapter "{chapter}"). Write for the student, in simple Indian English.
Bloom levels: Remember = recalling facts and terms, Understand = explaining ideas in their own words,
Apply = using an idea or formula in a new situation, Analyze = comparing, reasoning, finding causes.
Write level names like that (never in capitals).
Write:
- summary: one or two sentences on how they did overall - honest, encouraging, no score numbers.
- lagging_meaning: one sentence on what being weak at the lagging Bloom level means for THIS topic
  (e.g. "You know the formula but get stuck using it on a new circuit."). Empty if none is lagging.
- need_to_know: 2-3 short points (each under 20 words) - the ideas they still need, taken from the
  questions they missed. State the idea itself; never quote a question or say which option is right.
- next_step: one short sentence telling them what to do next ({next})."""


class MissedQuestion(BaseModel):
    bloom_level: str
    question: str = Field(max_length=2000)
    chosen: str = Field(max_length=500)
    correct: str = Field(max_length=500)


class AnalysisRequest(BaseModel):
    grade: int = Field(ge=1, le=12)
    subject: str = Field(max_length=200)
    chapter: str = Field(max_length=300)
    topic: str = Field(min_length=1, max_length=300)
    attempt: int = Field(ge=1, le=2)  # 2 = re-check after homework
    per_bloom: dict[str, int]
    previous_per_bloom: dict[str, int] | None = None
    lagging_level: str | None = None
    missed: list[MissedQuestion] = Field(default_factory=list, max_length=20)


class AnalysisResult(BaseModel):
    summary: str
    lagging_meaning: str
    need_to_know: list[str]
    next_step: str


def _next(body: AnalysisRequest) -> str:
    if body.lagging_level is None:
        return "the topic is done - point them to the next one"
    if body.attempt == 1:
        return "do the homework made for these gaps, then take the re-check quiz"
    return "go over these points with the revision cards; their teacher will help with the rest"


async def analyse(llm: StructuredLLM, body: AnalysisRequest, *, model: str) -> AnalysisResult:
    system = SYSTEM.format(grade=body.grade, subject=body.subject, topic=body.topic, chapter=body.chapter,
                           next=_next(body))
    # "Apply", not "APPLY" - the model copies the spelling it is given into what the student reads
    lines = [f"Attempt: {'re-check after homework' if body.attempt == 2 else 'first quiz'}",
             "Percent right per Bloom level: " + ", ".join(f"{k.title()} {v}%" for k, v in body.per_bloom.items())]
    if body.previous_per_bloom:
        lines.append("First quiz, for comparison: "
                     + ", ".join(f"{k.title()} {v}%" for k, v in body.previous_per_bloom.items()))
    lines.append(f"Lagging level: {body.lagging_level.title() if body.lagging_level else 'none'}")
    for n, m in enumerate(body.missed, start=1):
        lines.append(f"Missed {n} [{m.bloom_level.title()}]: {m.question}\n  chose: {m.chosen}\n  right: {m.correct}")
    result = await llm(model=model, effort="low", system=system, user="\n".join(lines), output_type=AnalysisResult)
    return AnalysisResult(
        summary=result.summary.strip(),
        lagging_meaning=result.lagging_meaning.strip() if body.lagging_level else "",
        need_to_know=[p.strip() for p in result.need_to_know if p.strip()][:3],
        next_step=result.next_step.strip(),
    )
