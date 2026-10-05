"""Writes the words of a student's post-test insights (core's
GET /me/tests/{id}/insights, contract C4). Core computes every number and
picks the cards; this only turns them into a headline, a key idea per card,
three next steps and a short spoken recap. One gpt-5-mini call with a
strict Structured Outputs schema; any failure raises so core can fall back
to its own template words.
"""
import json
import logging
from typing import Any, Optional

from openai import AsyncOpenAI

from src.config.settings import get_settings

logger = logging.getLogger(__name__)

INSIGHTS_MODEL = "gpt-5-mini"
# Per attempt; 2 retries stay under core's 30s wait on this call.
_TIMEOUT_SECONDS = 9.0

_client: Optional[AsyncOpenAI] = None

RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "test_insights",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "headline": {"type": "string"},
                "cards": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {"question_number": {"type": "integer"}, "key_idea": {"type": "string"}},
                        "required": ["question_number", "key_idea"],
                    },
                },
                "next_steps": {
                    "type": "array",
                    "minItems": 3,
                    "maxItems": 3,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {"title": {"type": "string"}, "reference": {"type": "string"}},
                        "required": ["title", "reference"],
                    },
                },
                "recap_text": {"type": "string"},
            },
            "required": ["headline", "cards", "next_steps", "recap_text"],
        },
    },
}

SYSTEM_PROMPT = """You are Milo, a friendly study buddy for an Indian school student (Class 8-10). Write in simple Indian English a 13-year-old understands.
You get the student's numbers from a spoken test and the questions they found hard. Write only words; never change or invent numbers.
- headline: one sentence naming the strongest and the weakest Bloom level (use plain names: Remember, Understand, Apply, Analyze, Evaluate, Create).
- cards: for every given question, key_idea = 1-2 sentences stating the core idea the student should hold, built from the expected and missed points.
- next_steps: exactly 3 items; title = a short concrete action; reference = where to look, textbook-style, using the given topic/subtopic names.
- recap_text: about 30 seconds when spoken (70-90 words), warm and encouraging, addressed to the student as "you". Never say "right", "wrong", "correct" or "incorrect"."""


def _get_client() -> Optional[AsyncOpenAI]:
    global _client
    key = get_settings().openai_api_key
    if _client is None and key:
        _client = AsyncOpenAI(api_key=key, timeout=_TIMEOUT_SECONDS, max_retries=2)
    return _client


async def generate_insights(facts: dict[str, Any], client: Optional[Any] = None) -> dict[str, Any]:
    """facts = core's request body (numbers + hard cards). Returns
    {headline, cards:[{question_number, key_idea}], next_steps:[{title, reference}], recap_text}."""
    client = client or _get_client()
    if client is None:
        raise RuntimeError("openai_api_key is not configured")
    resp = await client.chat.completions.create(
        model=INSIGHTS_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(facts, ensure_ascii=False)},
        ],
        response_format=RESPONSE_FORMAT,
    )
    data = json.loads(resp.choices[0].message.content)
    data["next_steps"] = data["next_steps"][:3]  # schema says exactly 3; core pads if fewer
    return data
