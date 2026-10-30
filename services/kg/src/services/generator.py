"""KG-grounded question generation: one OpenAI call, output constrained to
a strict JSON schema (Structured Outputs, json_schema format/strict mode)
so the response is always valid, parseable JSON.

The retrieval step (what context to ground the call in) lives in
nool_kg_client.get_concept_context_for_scope; this module only turns that
context plus a request shape into grounded questions.
"""

from __future__ import annotations

import json
import logging
import os
from functools import lru_cache

from openai import AsyncOpenAI

from shared import llm_gateway
from src.services import cost

logger = logging.getLogger(__name__)

# KG_MODEL swaps the paper/context writer (e.g. gpt-5-mini) once its eval holds up
MODEL = os.environ.get("KG_MODEL") or "gpt-5.1"
MAX_COMPLETION_TOKENS = 16000
# max_retries gives the SDK's own exponential backoff on 429/5xx/timeouts.
OPENAI_TIMEOUT_SECONDS = 90.0
OPENAI_MAX_RETRIES = 2


@lru_cache
def _get_client(api_key: str) -> AsyncOpenAI:
    """One shared AsyncOpenAI client (connection pool) per key instead of a
    new sync client per request blocking the event loop."""
    return AsyncOpenAI(**llm_gateway.client_kwargs(api_key), timeout=OPENAI_TIMEOUT_SECONDS,
                       max_retries=OPENAI_MAX_RETRIES)

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
                    "slot_index": {"type": ["integer", "null"]},
                    "options": {"type": ["array", "null"], "items": {"type": "string"}},
                },
                "required": [
                    "text", "answer", "bloom_level", "difficulty", "question_type",
                    "chapter_number", "topic", "source_refs", "slot_index", "options",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["questions"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You are a {board} Class {grade} {subject} question-paper writer. You are given a set
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
entries. Spread questions across ALL the provided topics as evenly as the
requested count allows - do not concentrate on the first or introductory
topic.

Every question must be self-contained text a student can answer from the
printed paper alone: never refer to a figure, diagram, table, activity or
page "in your textbook" (no "Fig. 2.3", "the circuit shown", "the table
above"). If the idea needs a diagram, describe the setup in words instead.

- Marks decide the amount of work (the slot's `marks`): 1 mark = one fact or one
  step; 2 marks = at most two short things to state or one two-step working;
  3 marks = at most three points; never several sub-tasks, "explain in each
  case" or multi-part questions for 1-3 marks. A question with two or more parts or
  several steps is MEDIUM or HARD, never EASY.
- Difficulty (the slot's `difficulty`) means: EASY = direct recall or a
  one-step substitution; MEDIUM = apply a rule or formula to a new situation;
  HARD = several steps or combining two ideas. Pitch every question at
  Class {grade} board level, never lower-class content.
- Never say "as described in the text/chapter/lesson", "mentioned in the
  chapter" or similar: the student only sees the question.
- options: for mcq exactly 4 options (without "(a)" labels), all different,
  plausible and mutually exclusive: no option may contain or imply another,
  contradict itself ("A will happen and B will happen too") or be an "all/none of
  the above" type; `answer` is
  one of them word for word; the options go only here, never in `text`.
  null for every other question_type.
- slot_index: in slot mode, the slot_index of the slot this question fills;
  null otherwise.
- If the given topics cannot support a slot's Bloom level or type without
  outside facts, write the closest question the content does support at that
  level and add "level_stretched" to source_refs."""

VERIFY_MODEL = os.environ.get("KG_VERIFY_MODEL") or "gpt-5-mini"

VERIFY_SCHEMA = {
    "type": "object",
    "properties": {"verdicts": {"type": "array", "items": {
        "type": "object",
        "properties": {"index": {"type": "integer"}, "ok": {"type": "boolean"}, "problem": {"type": "string"},
                       "difficulty": {"type": "string", "enum": list(_DIFFICULTY_LEVELS)}},
        "required": ["index", "ok", "problem", "difficulty"],
        "additionalProperties": False,
    }}},
    "required": ["verdicts"],
    "additionalProperties": False,
}

VERIFY_SYSTEM = """You are a strict {board} Class {grade} {subject} chief examiner checking questions
before they are printed. For each question, solve it yourself first, then compare with its `answer`.
ok=true only if ALL hold:
1. A student can answer it from the printed text alone (no figure or textbook reference).
2. It has exactly one correct answer, and `answer` is that answer. For mcq, exactly one of `options`
   is correct and `answer` is that option. For assertion-reason, decide separately whether A is true,
   whether R is true, and whether R explains A, then pick the option.
3. Numbers and facts are right (recompute every calculation).
4. The work asked fits the `marks` (a 1-3 mark question has at most 1-3 things to state) and it is at
   Class {grade} level, not a lower class. A wrong `difficulty` label is NOT a reason to fail a question.
5. For mcq the four options are different, mutually exclusive, and none contradicts itself.
6. It never says "as described in the text/chapter" or points at anything the printed paper does not show.
If not ok, say precisely what is wrong in `problem`; otherwise leave it empty.
In `difficulty` always give the label that fits the question (EASY = direct recall or one step; MEDIUM = apply a
rule to a new situation or two short parts; HARD = several steps or two ideas combined), whether or not ok."""


# ponytail: the 20-30% bank share is prompt-enforced, not checked after the
# fact - add a similarity check against reference texts if it drifts.
REFERENCE_GUIDANCE = """
The teacher selected question-bank datasets for this paper. Below is a
sample of their questions on the chosen topics (previous-year / sample-paper
style). Use them as grounding for scope, style, difficulty and marking:
- Take 20-30% of the paper (at least 1 question) straight from this bank -
  verbatim, or with only minimal edits - picking ones that fit a requested
  Bloom/difficulty slot. For each of these put "dataset" in source_refs
  alongside the topic_ref.
- Every other question must be NEW: a different fact, scenario, numbers or
  angle on the same syllabus content - not a paraphrase of a bank question.
- Never include the same underlying question twice.
- Never take a bank question that needs a figure, diagram or table to answer.
- Any fact in a new question must still come from the textbook context
  above or from these bank questions/answers - nothing outside them.

Question-bank sample:"""


# slot mode: core already copied its bank questions into the paper, so the
# sample is only for style - every slot here gets a NEW question
BANK_STYLE_GUIDANCE = """
Question-bank sample on these topics (previous-year / sample-paper style) -
match its scope, style, difficulty and marking, but write NEW questions: a
different fact, scenario, numbers or angle, never a copy or paraphrase.

Question-bank sample:"""


def _build_user_message(
    *,
    concept_context: list[dict],
    bloom_distribution: dict[str, int],
    difficulty_distribution: dict[str, int],
    question_types: list[str],
    total_questions: int,
    reference_questions: list[dict] | None = None,
    slots: list[dict] | None = None,
) -> str:
    if slots:
        return "\n".join([
            f"Generate exactly {len(slots)} questions, IN THIS ORDER, one per slot below. Each question "
            "must match its slot's bloom_level, difficulty and question_type, and its length and depth "
            "must suit its marks (1 mark = one quick fact or MCQ; 5 marks = a multi-step answer). "
            "A slot that names a chapter must be answered from that chapter only (set chapter_number to it).",
            json.dumps([{"slot_index": i, **slot} for i, slot in enumerate(slots)]),
            "\nAvailable topics/subtopics (grounding context):",
            json.dumps(concept_context, indent=2),
            *([BANK_STYLE_GUIDANCE, json.dumps(reference_questions, indent=2)] if reference_questions else []),
        ])
    parts = [
        f"Generate exactly {total_questions} questions.",
        f"Bloom level distribution (level: count): {json.dumps(bloom_distribution)}",
        f"Difficulty distribution (level: count): {json.dumps(difficulty_distribution)}",
        f"Allowed question types: {question_types or list(_QUESTION_TYPES)}",
        "\nAvailable topics/subtopics (grounding context):",
        json.dumps(concept_context, indent=2),
    ]
    if reference_questions:
        parts += [REFERENCE_GUIDANCE, json.dumps(reference_questions, indent=2)]
    return "\n".join(parts)


ASSESSOR_CONTENT_SCHEMA = {
    "type": "object",
    "properties": {"textbook_context": {"type": "string"}},
    "required": ["textbook_context"],
    "additionalProperties": False,
}

ASSESSOR_SYSTEM_PROMPT = """You are writing grounding material for a spoken "Co-Learner & Study Companion" -
an AI that has a live voice conversation with a student about a chapter.

You are given topics/subtopics retrieved from the actual NCERT textbook -
each with its own textbook text, worked examples, and activities. Produce
`textbook_context`: a compact prose/bullet summary of the grounding
topics/subtopics (key facts, formulas, relationships) written in plain
spoken-friendly language - no LaTeX, no raw symbols - suitable to hand
directly to that conversational agent as its factual grounding. Use only
the given content - do not introduce facts, numbers, or terminology not
present in it. Do not write any questions."""


async def generate_assessor_content(*, api_key: str, concept_context: list[dict]) -> dict:
    """Grounded spoken-friendly textbook context for a Voice Test. The test's
    questions now come from the school's question datasets via the
    generation service, so this no longer invents any: reference_questions
    is always [] (kept for core's kg_client contract).
    """
    if not concept_context:
        return {"reference_questions": [], "textbook_context": ""}

    response = await _get_client(api_key).chat.completions.create(
        model=MODEL,
        max_completion_tokens=MAX_COMPLETION_TOKENS,
        **llm_gateway.call_kwargs("chat"),
        messages=[
            {"role": "system", "content": ASSESSOR_SYSTEM_PROMPT},
            {"role": "user", "content": "Grounding topics/subtopics:\n" + json.dumps(concept_context, indent=2)},
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

    cost.log_chat_usage(MODEL, getattr(response, "usage", None))
    message = response.choices[0].message
    if message.refusal or message.content is None:
        raise RuntimeError(f"Model refused or returned no content: {message.refusal}")
    return {"reference_questions": [], "textbook_context": json.loads(message.content)["textbook_context"]}


async def generate_questions(
    *,
    api_key: str,
    board: str,
    grade: int,
    subject: str,
    concept_context: list[dict],
    bloom_distribution: dict[str, int],
    difficulty_distribution: dict[str, int],
    question_types: list[str],
    total_questions: int,
    reference_questions: list[dict] | None = None,
    slots: list[dict] | None = None,
) -> list[dict]:
    if not concept_context and not reference_questions:
        return []

    response = await _get_client(api_key).chat.completions.create(
        model=MODEL,
        max_completion_tokens=MAX_COMPLETION_TOKENS,
        **llm_gateway.call_kwargs("chat"),
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT.format(board=board, grade=grade, subject=subject)},
            {
                "role": "user",
                "content": _build_user_message(
                    concept_context=concept_context,
                    bloom_distribution=bloom_distribution,
                    difficulty_distribution=difficulty_distribution,
                    question_types=question_types,
                    total_questions=total_questions,
                    reference_questions=reference_questions,
                    slots=slots,
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

    cost.log_chat_usage(MODEL, getattr(response, "usage", None))
    message = response.choices[0].message
    if message.refusal or message.content is None:
        raise RuntimeError(f"Model refused or returned no content: {message.refusal}")
    questions = json.loads(message.content)["questions"]
    return await _verified(api_key, questions, board=board, grade=grade, subject=subject, slots=slots)


async def _verified(api_key: str, questions: list[dict], *, board: str, grade: int, subject: str,
                    slots: list[dict] | None = None) -> list[dict]:
    """Drops every question whose answer a second model, solving it on its
    own, doesn't agree with (C-009: three wrong MCQ keys reached papers)."""
    if not questions:
        return questions
    def marks_of(q: dict):
        slot = q.get("slot_index")
        return slots[slot].get("marks") if slots and isinstance(slot, int) and 0 <= slot < len(slots) else None

    items = [{"index": i, **{k: q.get(k) for k in ("text", "options", "answer", "question_type", "difficulty")},
              "marks": marks_of(q)} for i, q in enumerate(questions)]
    try:
        response = await _get_client(api_key).chat.completions.create(
            model=VERIFY_MODEL,
            max_completion_tokens=MAX_COMPLETION_TOKENS,
            **llm_gateway.call_kwargs("chat"),
            messages=[
                {"role": "system", "content": VERIFY_SYSTEM.format(board=board, grade=grade, subject=subject)},
                {"role": "user", "content": json.dumps(items, ensure_ascii=False)},
            ],
            response_format={"type": "json_schema", "json_schema": {
                "name": "question_verdicts", "schema": VERIFY_SCHEMA, "strict": True}},
        )
        cost.log_chat_usage(VERIFY_MODEL, getattr(response, "usage", None))
        verdicts = json.loads(response.choices[0].message.content)["verdicts"]
    except Exception as exc:
        # ponytail: a verifier outage keeps the batch (the answer-key step verifies again); drop instead if keys still slip
        logger.warning("kg_verify_failed", extra={"error": type(exc).__name__})
        return questions
    bad = {v["index"]: v["problem"] for v in verdicts if not v["ok"]}
    for i, why in bad.items():
        # the message carries the reason: this service's log lines don't print `extra`
        logger.info(f"kg_question_dropped slot={questions[i].get('slot_index')} problem={why[:200]!r}")
    # B26: a wrong difficulty label used to fail the question - and a small slot asked HARD failed on every re-ask,
    # so papers came back short. The question stays, labelled as the examiner judges it.
    fitted = {v["index"]: v.get("difficulty") for v in verdicts if v["ok"] and v.get("difficulty")}
    kept = []
    for i, q in enumerate(questions):
        if i not in bad:
            if fitted.get(i) and fitted[i] != q.get("difficulty"):
                logger.info(f"kg_question_relabelled slot={q.get('slot_index')} {q.get('difficulty')}->{fitted[i]}")
                q = {**q, "difficulty": fitted[i]}
            kept.append(q)
    return kept
