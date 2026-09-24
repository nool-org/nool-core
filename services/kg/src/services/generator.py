"""KG-grounded question generation: one OpenAI call, output constrained to
a strict JSON schema (Structured Outputs, json_schema format/strict mode)
so the response is always valid, parseable JSON.

The retrieval step (what context to ground the call in) lives in
nool_kg_client.get_concept_context_for_scope; this module only turns that
context plus a request shape into grounded questions.
"""

from __future__ import annotations

import json

from openai import OpenAI

MODEL = "gpt-5.1"
MAX_COMPLETION_TOKENS = 16000

_BLOOM_LEVELS = ("REMEMBER", "UNDERSTAND", "APPLY", "ANALYZE", "EVALUATE", "CREATE")
_DIFFICULTY_LEVELS = ("EASY", "MEDIUM", "HARD")
_QUESTION_TYPES = ("mcq", "short_answer", "long_answer", "numerical")

GENERATED_QUESTIONS_SCHEMA = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "answer": {"type": "string"},
                    "bloom_level": {"type": "string", "enum": list(_BLOOM_LEVELS)},
                    "difficulty": {"type": "string", "enum": list(_DIFFICULTY_LEVELS)},
                    "question_type": {"type": "string", "enum": list(_QUESTION_TYPES)},
                    "chapter_number": {"type": "integer"},
                    "topic": {"type": "string"},
                    "source_refs": {"type": "array", "items": {"type": "string"}},
                },
                "required": [
                    "text", "answer", "bloom_level", "difficulty", "question_type",
                    "chapter_number", "topic", "source_refs",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["questions"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You are a CBSE Class 10 Science question-paper writer. You are given a set
of topics/subtopics retrieved from the actual NCERT textbook - each with its
own textbook text, activities, worked examples, and existing textbook
questions, grounded with a topic_ref you must cite in source_refs.

Write exam-quality questions strictly grounded in the given content - do not
introduce facts, numbers, or terminology not present in the provided context.
Match the requested Bloom level (REMEMBER/UNDERSTAND/APPLY/ANALYZE/EVALUATE/
CREATE) and difficulty (EASY/MEDIUM/HARD) for each question as closely as
the content allows, and the requested question_type. Every question must
cite the topic_ref(s) it draws from in source_refs and the correct
chapter_number/topic it belongs to. Provide a concise reference answer for
each question. Avoid duplicating the same fact/question across different
entries - cover distinct topics/subtopics where the requested count allows
it."""


def _build_user_message(
    *,
    concept_context: list[dict],
    bloom_distribution: dict[str, int],
    difficulty_distribution: dict[str, int],
    question_types: list[str],
    total_questions: int,
) -> str:
    parts = [
        f"Generate exactly {total_questions} questions.",
        f"Bloom level distribution (level: count): {json.dumps(bloom_distribution)}",
        f"Difficulty distribution (level: count): {json.dumps(difficulty_distribution)}",
        f"Allowed question types: {question_types or list(_QUESTION_TYPES)}",
        "\nAvailable topics/subtopics (grounding context):",
        json.dumps(concept_context, indent=2),
    ]
    return "\n".join(parts)


ASSESSOR_CONTENT_SCHEMA = {
    "type": "object",
    "properties": {
        "reference_questions": {
            "type": "array",
            "items": {"type": "string"},
        },
        "textbook_context": {"type": "string"},
    },
    "required": ["reference_questions", "textbook_context"],
    "additionalProperties": False,
}

ASSESSOR_SYSTEM_PROMPT = """You are writing grounding material for a spoken "Co-Learner & Study Companion" -
an AI that has a live voice conversation with a student, exploring a chapter
through creative real-world scenario questions rather than a written exam.

You are given topics/subtopics retrieved from the actual NCERT textbook -
each with its own textbook text, worked examples, and activities. Produce
two things, strictly grounded in the given content only - do not introduce
facts, numbers, or terminology not present in the provided context:

1. `reference_questions`: a list of creative, real-world scenario questions
   (household gadgets, appliances, vehicles, weather, sports, design
   puzzles) that test the target Bloom's levels - inspirational raw
   material a live conversational agent will draw from and rephrase on the
   fly, not a fixed script to recite verbatim. Match the requested count.
2. `textbook_context`: a compact prose/bullet summary of the grounding
   topics/subtopics (key facts, formulas, relationships) written in plain
   spoken-friendly language - no LaTeX, no raw symbols - suitable to hand
   directly to that conversational agent as its factual grounding."""


def _build_assessor_user_message(
    *, concept_context: list[dict], bloom_levels: list[str], num_questions: int
) -> str:
    parts = [
        f"Generate {num_questions} reference scenario questions.",
        f"Target Bloom's Taxonomy levels: {json.dumps(bloom_levels)}",
        "\nAvailable topics/subtopics (grounding context):",
        json.dumps(concept_context, indent=2),
    ]
    return "\n".join(parts)


def generate_assessor_content(
    *, api_key: str, concept_context: list[dict], bloom_levels: list[str], num_questions: int
) -> dict:
    """Grounded reference questions + a spoken-friendly context summary for
    the colearner voice pipeline's SessionConfig (reference_questions/
    textbook_context - see services/colearner/src/domain/schemas.py). Same
    one-call Structured-Outputs approach as generate_questions above; a
    distinct schema/prompt since this is scenario material for a live
    conversation, not written exam questions with answers/difficulty/type.
    """
    if not concept_context:
        return {"reference_questions": [], "textbook_context": ""}

    client = OpenAI(api_key=api_key)
    response = client.chat.completions.create(
        model=MODEL,
        max_completion_tokens=MAX_COMPLETION_TOKENS,
        messages=[
            {"role": "system", "content": ASSESSOR_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": _build_assessor_user_message(
                    concept_context=concept_context, bloom_levels=bloom_levels, num_questions=num_questions
                ),
            },
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "generated_assessor_content",
                "schema": ASSESSOR_CONTENT_SCHEMA,
                "strict": True,
            },
        },
    )

    message = response.choices[0].message
    if message.refusal or message.content is None:
        raise RuntimeError(f"Model refused or returned no content: {message.refusal}")
    return json.loads(message.content)


def generate_questions(
    *,
    api_key: str,
    concept_context: list[dict],
    bloom_distribution: dict[str, int],
    difficulty_distribution: dict[str, int],
    question_types: list[str],
    total_questions: int,
) -> list[dict]:
    if not concept_context:
        return []

    client = OpenAI(api_key=api_key)
    response = client.chat.completions.create(
        model=MODEL,
        max_completion_tokens=MAX_COMPLETION_TOKENS,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": _build_user_message(
                    concept_context=concept_context,
                    bloom_distribution=bloom_distribution,
                    difficulty_distribution=difficulty_distribution,
                    question_types=question_types,
                    total_questions=total_questions,
                ),
            },
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "generated_questions",
                "schema": GENERATED_QUESTIONS_SCHEMA,
                "strict": True,
            },
        },
    )

    message = response.choices[0].message
    if message.refusal or message.content is None:
        raise RuntimeError(f"Model refused or returned no content: {message.refusal}")
    return json.loads(message.content)["questions"]
