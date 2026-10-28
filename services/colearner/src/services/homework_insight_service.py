"""Generates the real, LLM-grounded "why this Homework, and how to close
the gap" content shown on the student's Homework details screen (nool-apps'
homework-learn.tsx) - see src/api/routes/homework_insight.py for the
stateless REST endpoint Core calls this behind.

A single gpt-4o-mini call with strict Structured Outputs (RESPONSE_FORMAT),
with a deterministic, clearly-labeled fallback if the call fails
(no API key configured, rate limited, network error, malformed JSON) so a
slow/broken LLM never breaks the Homework screen, it just falls back to a
generic paragraph the same shape as before this feature existed.
"""

import json
import logging

from openai import AsyncOpenAI

from shared import llm_gateway

from src.config.settings import get_settings

LLM_MODEL = "gpt-4o-mini"
# Per attempt; 3 attempts (max_retries=2) + backoff stay under the 30s
# Core waits on this call (colearner_insight_client.py).
_TIMEOUT_SECONDS = 8.0

logger = logging.getLogger(__name__)

_settings = get_settings()
openai_client = (
    AsyncOpenAI(**llm_gateway.client_kwargs(_settings.openai_api_key), timeout=_TIMEOUT_SECONDS, max_retries=2)
    if _settings.openai_api_key
    else None
)


_STR = {"type": "string"}
RESPONSE_FORMAT = {"type": "json_schema", "json_schema": {"name": "homework_insight", "strict": True, "schema": {
    "type": "object", "additionalProperties": False,
    "required": ["what_needs_understanding", "references", "key_idea_title", "key_idea_body", "connection_prompt"],
    "properties": {
        "what_needs_understanding": _STR,
        "references": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                                  "required": ["title", "subtitle"],
                                                  "properties": {"title": _STR, "subtitle": _STR}}},
        "key_idea_title": _STR, "key_idea_body": _STR, "connection_prompt": _STR}}}}

SYSTEM_PROMPT = """You are Milo, a warm study coach for Indian CBSE students aged 14-18. Write in simple Indian English.
<student> gives a student's weak spot after a test; <syllabus> is the textbook excerpt for it. Write:
- what_needs_understanding: 1 sentence naming the specific idea they need (not "practise more").
- references: 1-2 items; title = a section or concept name that appears in <syllabus>, subtitle = why, in under 8 words. If <syllabus> has no excerpt, use the topic name as the only title - never invent section names.
- key_idea_title: the concept name. key_idea_body: 2-3 sentences explaining it, using only <syllabus> (or standard content for the student's grade if it has no excerpt).
- connection_prompt: one everyday Indian example (under 25 words) linking the idea to the student's life.
Never mention scores or percentages."""


def _fallback(topic: str) -> dict:
    return {
        "what_needs_understanding": f"You need more practice with {topic}.",
        "references": [{"title": "Class notes", "subtitle": topic}],
        "key_idea_title": topic,
        "key_idea_body": f"A quick refresher on {topic}.",
        "connection_prompt": f"How does {topic} connect to what you already know?",
    }


async def generate_homework_insight(
    *,
    topic: str,
    grade: str,
    subject: str,
    mastery_percent: int,
    weak_bloom_level: str,
    textbook_context: str | None,
) -> dict:
    if not openai_client:
        return _fallback(topic)

    context_text = (textbook_context or "").strip() or (
        f"No specific syllabus excerpt was provided - ground your explanation in standard "
        f"{grade} {subject} coverage of {topic}."
    )

    # static instructions first (cacheable), the student's data last
    user = (f"<student>grade={grade}; subject={subject}; topic={topic}; mastery={mastery_percent}%; "
            f"weakest_bloom={weak_bloom_level}</student>\n<syllabus>{context_text}</syllabus>")

    try:
        resp = await openai_client.chat.completions.create(
            model=LLM_MODEL,
            **llm_gateway.call_kwargs("chat"),
            messages=[{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}],
            response_format=RESPONSE_FORMAT,
        )
        msg = resp.choices[0].message
        if getattr(msg, "refusal", None) or not msg.content:
            raise RuntimeError(f"homework insight refused or empty: {getattr(msg, 'refusal', None)}")
        data = json.loads(msg.content)
        fallback = _fallback(topic)
        references = [r for r in data.get("references") or []
                      if isinstance(r, dict) and isinstance(r.get("title"), str) and r["title"].strip()][:2]
        return {
            "what_needs_understanding": data.get("what_needs_understanding") or fallback["what_needs_understanding"],
            "references": references or fallback["references"],
            "key_idea_title": data.get("key_idea_title") or fallback["key_idea_title"],
            "key_idea_body": data.get("key_idea_body") or fallback["key_idea_body"],
            "connection_prompt": data.get("connection_prompt") or fallback["connection_prompt"],
        }
    except Exception:
        logger.exception("homework_insight_generation_failed")
        return _fallback(topic)
