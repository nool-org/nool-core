"""Revision cards: 4-8 short cards that sum up one textbook topic - its key
ideas, definitions and formulas, one common mistake and one or two quick
self-check questions. Core generates a topic's deck once and shows it to every
student (services/core/src/services/revision_cards.py).

One VERIFY_MODEL (gpt-5-mini, low effort) call grounded in the topic's
textbook text, then code checks (card count, non-empty, body length, a check
card with its answer, nothing to write or draw). A failing deck is retried
once with the problems listed; still failing -> RevisionCardsError.
"""

import re
from typing import Literal

from pydantic import BaseModel, Field

from src.services.llm import StructuredLLM

MIN_CARDS, MAX_CARDS = 4, 8  # 4: a thin topic gets fewer cards, never padding from outside the text
MAX_TITLE = 80
MAX_BODY = 400
MAX_ANSWER = 250
MAX_CONTEXT = 12000

SYSTEM = """You write revision cards for CBSE Class {grade} {subject} students in India, for one topic of
the chapter "{chapter}". A student flips through them in a minute or two before a test.
Write {min_cards}-{max_cards} cards, in this order:
- mostly "concept", "definition" and "formula" cards covering the topic's key ideas (a formula card only
  when the topic really has a formula or equation),
- exactly one "mistake" card: a common mistake or misconception students make in this topic, and the fix,
- one or two "check" cards: a quick self-check question in body, and its short answer in answer.
Rules:
- Use ONLY what the textbook text says - no facts from outside it. If the text is too thin for more,
  write fewer concept cards rather than adding anything it doesn't say.
- A "check" card's answer must be stated in the text.
- title: a few words. body: 2-3 short sentences, plain text in simple Indian English, under 60 words.
- Maths and formulas in LaTeX inside $...$ (e.g. $v = u + at$). No markdown, no bullet lists.
- answer: only for "check" cards (one short sentence or value); null for every other card.
- Nothing to write, draw, label or tabulate - the student reads the card and thinks."""

_ASK = r"(?:write|draw|label|tabulate|sketch)\b"
# a check question must not ask for it at all; other cards may mention it ("students often
# write...") but must not tell the student to do it
_WRITTEN_ASK = re.compile(rf"\b{_ASK}", re.IGNORECASE)
_WRITTEN_INSTRUCTION = re.compile(rf"(?:^|[.!?]\s+){_ASK}", re.IGNORECASE)


class CardsRequest(BaseModel):
    grade: int = Field(ge=1, le=12)
    subject: str = Field(max_length=200)
    chapter: str = Field(max_length=300)
    topic: str = Field(min_length=1, max_length=300)
    context: str = Field(min_length=1, max_length=MAX_CONTEXT * 4)


class Card(BaseModel):
    kind: Literal["concept", "definition", "formula", "mistake", "check"]
    title: str
    body: str
    answer: str | None = None


class CardsResult(BaseModel):
    cards: list[Card]


class RevisionCardsError(Exception):
    pass


def problems(cards: list[Card]) -> list[str]:
    """What's wrong with a deck - empty when it can be shown."""
    found = []
    if not MIN_CARDS <= len(cards) <= MAX_CARDS:
        found.append(f"write {MIN_CARDS}-{MAX_CARDS} cards, not {len(cards)}")
    if not any(c.kind == "check" and (c.answer or "").strip() for c in cards):
        found.append('add a "check" card with its answer')
    for n, c in enumerate(cards, start=1):
        if not c.title.strip() or not c.body.strip():
            found.append(f"card {n} is empty")
        if len(c.title) > MAX_TITLE or len(c.body) > MAX_BODY or len(c.answer or "") > MAX_ANSWER:
            found.append(f"card {n} is too long")
        if (_WRITTEN_ASK if c.kind == "check" else _WRITTEN_INSTRUCTION).search(c.body):
            found.append(f"card {n} asks the student to write or draw something")
    return found


def tidy(card: Card) -> Card:
    answer = (card.answer or "").strip() if card.kind == "check" else ""
    return Card(kind=card.kind, title=card.title.strip(), body=card.body.strip(), answer=answer or None)


async def write_cards(llm: StructuredLLM, body: CardsRequest, *, model: str) -> CardsResult:
    system = SYSTEM.format(grade=body.grade, subject=body.subject, chapter=body.chapter,
                           min_cards=MIN_CARDS, max_cards=MAX_CARDS)
    user = f"Topic: {body.topic}\nTEXTBOOK TEXT:\n{body.context[:MAX_CONTEXT]}"
    for attempt in range(2):
        result = await llm(model=model, effort="low", system=system, user=user, output_type=CardsResult)
        cards = [tidy(c) for c in result.cards]
        found = problems(cards)
        if not found:
            return CardsResult(cards=cards)
        user += "\n\nYour last cards had these problems - fix them:\n- " + "\n- ".join(found)
    raise RevisionCardsError("; ".join(found))
