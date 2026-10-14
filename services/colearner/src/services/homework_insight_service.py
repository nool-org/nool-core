"""Generates the real, LLM-grounded "why this Homework, and how to close
the gap" content shown on the student's Homework details screen (nool-apps'
homework-learn.tsx) - see src/api/routes/homework_insight.py for the
stateless REST endpoint Core calls this behind.

Reuses the same JSON-mode grading-call pattern as chained_service.py's
generate_bloom_report - a single gpt-4o-mini call producing structured
JSON, with a deterministic, clearly-labeled fallback if the call fails
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

    prompt = f"""A student in {grade} just finished a {subject} test on '{topic}' and scored {mastery_percent}% overall, weakest on the '{weak_bloom_level}' Bloom's Taxonomy level. Generate real, specific homework guidance - not generic filler - adhering strictly to this JSON schema:

```json
{{
  "what_needs_understanding": "<1 sentence, specific to their actual weak spot - not just 'practice more'>",
  "references": [
    {{"title": "<a specific section/concept name from the context below, not 'Class notes'>", "subtitle": "<why this one, in a few words>"}}
  ],
  "key_idea_title": "<the core concept name>",
  "key_idea_body": "<2-3 sentences clearly explaining that concept, grounded in the context below>",
  "connection_prompt": "<one concrete real-world analogy or everyday scenario connecting this concept to something the student already experiences>"
}}
```

SYLLABUS CONTEXT:
\"\"\"
{context_text}
\"\"\"

Return 1-2 references. Be concrete and grounded in the context above, not vague ("this chapter", "the material") - name the actual idea.
"""

    try:
        resp = await openai_client.chat.completions.create(
            model=LLM_MODEL,
            **llm_gateway.call_kwargs("chat"),
            messages=[
                {
                    "role": "system",
                    "content": "You are a precise, encouraging study coach. Output strictly valid JSON.",
                },
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
        )
        data = json.loads(resp.choices[0].message.content)
        fallback = _fallback(topic)
        references = data.get("references")
        return {
            "what_needs_understanding": data.get("what_needs_understanding") or fallback["what_needs_understanding"],
            "references": references if isinstance(references, list) and references else fallback["references"],
            "key_idea_title": data.get("key_idea_title") or fallback["key_idea_title"],
            "key_idea_body": data.get("key_idea_body") or fallback["key_idea_body"],
            "connection_prompt": data.get("connection_prompt") or fallback["connection_prompt"],
        }
    except Exception:
        logger.exception("homework_insight_generation_failed")
        return _fallback(topic)
