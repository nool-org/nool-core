"""Teacher assistant: chat replies and the dashboard summary, both grounded
ONLY in the facts snapshot core sends (core's services/teacher_context.py) -
the model never invents numbers, students or tests."""

import json

from pydantic import BaseModel, Field

from src.services.llm import StructuredLLM

MAX_HISTORY = 12

RULES = """You are Nool's assistant for a school teacher in India. You help them understand
their classes and decide what to do next. Use ONLY the FACTS JSON: never invent a number,
student, test or paper. If the facts can't answer, say so in one line and suggest where in
the app to look (Tests, Homework, Papers, a test's Results). Be brief and practical - short
sentences, plain English, no markdown headings. Name students and chapters as they appear;
never assume a student's gender - use their name or "they".
Percentages are class averages from voice tests unless the facts say otherwise. Bloom levels:
REMEMBER, UNDERSTAND, APPLY, ANALYZE, EVALUATE, CREATE - call them Remember, Understand, etc."""


class ChatTurn(BaseModel):
    role: str  # "teacher" | "assistant"
    text: str


class ReplyRequest(BaseModel):
    facts: dict
    history: list[ChatTurn] = Field(default_factory=list)
    message: str = Field(min_length=1, max_length=2000)


class ReplyResult(BaseModel):
    reply: str = Field(description="2-6 short sentences, or up to 5 short bullet lines starting with '- '")


class SummaryRequest(BaseModel):
    facts: dict


class SummaryResult(BaseModel):
    bullets: list[str] = Field(description="2-3 bullets, each under 18 words, most important first")


async def reply(llm: StructuredLLM, body: ReplyRequest, *, model: str) -> ReplyResult:
    history = "\n".join(f"{t.role}: {t.text}" for t in body.history[-MAX_HISTORY:])
    user = (f"FACTS:\n{json.dumps(body.facts)}\n\nCONVERSATION SO FAR:\n{history or '(none)'}"
            f"\n\nTEACHER: {body.message}")
    return await llm(model=model, effort="low", system=RULES, user=user, output_type=ReplyResult)


async def summary(llm: StructuredLLM, body: SummaryRequest, *, model: str) -> SummaryResult:
    system = RULES + """
Write the teacher's dashboard summary: 2-3 bullets, most important first - what needs their
action today (reviews waiting, a test whose deadline passed with students not finished, low
homework completion), then the clearest learning signal (the weakest thinking level or a
student who needs help). No greetings, no filler."""
    return await llm(model=model, effort="low", system=system, user=f"FACTS:\n{json.dumps(body.facts)}",
                     output_type=SummaryResult)
