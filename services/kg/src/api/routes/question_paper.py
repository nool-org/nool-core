from fastapi import APIRouter

from shared.errors import ValidationError

from src.api.schemas import (
    GenerateQuestionsIn,
    GenerateQuestionsOut,
    GeneratedQuestionOut,
    ReplacementCandidatesIn,
)
from src.config.settings import get_settings
from src.services.generator import generate_questions
from src.services.nool_kg_client import get_concept_context_for_scope

router = APIRouter(prefix="/question-papers", tags=["question-papers"])


@router.post("/generate", response_model=GenerateQuestionsOut)
async def generate(body: GenerateQuestionsIn) -> GenerateQuestionsOut:
    settings = get_settings()
    if not settings.openai_api_key:
        raise ValidationError("OPENAI_API_KEY is not configured on the kg service.")

    context = await get_concept_context_for_scope(
        chapter_numbers=[c.number for c in body.chapters],
        topic_names=body.topic_names or None,
        subject=body.subject,
        board=body.board,
        grade=body.grade,
    )
    questions = generate_questions(
        api_key=settings.openai_api_key,
        concept_context=context,
        bloom_distribution=body.bloom_distribution,
        difficulty_distribution=body.difficulty_distribution,
        question_types=body.question_types,
        total_questions=body.total_questions,
    )
    return GenerateQuestionsOut(questions=[GeneratedQuestionOut(**q) for q in questions])


@router.post("/replacement-candidates", response_model=GenerateQuestionsOut)
async def replacement_candidates(body: ReplacementCandidatesIn) -> GenerateQuestionsOut:
    """Backs the paper Review screen's "Shuffle" picker - same generation
    core as /generate, asked for a small batch at one Bloom/difficulty
    target, with the question being replaced excluded from grounding so it
    doesn't just get repeated back.
    """
    settings = get_settings()
    if not settings.openai_api_key:
        raise ValidationError("OPENAI_API_KEY is not configured on the kg service.")

    context = await get_concept_context_for_scope(
        chapter_numbers=[c.number for c in body.chapters],
        topic_names=body.topic_names or None,
        subject=body.subject,
        board=body.board,
        grade=body.grade,
    )
    questions = generate_questions(
        api_key=settings.openai_api_key,
        concept_context=context,
        bloom_distribution={body.bloom_level: body.count},
        difficulty_distribution={body.difficulty: body.count},
        question_types=[],
        total_questions=body.count,
    )
    questions = [q for q in questions if q["text"] != body.exclude_text]
    return GenerateQuestionsOut(questions=[GeneratedQuestionOut(**q) for q in questions])
