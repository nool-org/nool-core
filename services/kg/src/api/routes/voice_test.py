from fastapi import APIRouter

from shared.errors import ValidationError

from src.api.schemas import GenerateAssessorContentIn, GenerateAssessorContentOut
from src.config.settings import get_settings
from src.services.generator import generate_assessor_content
from src.services.nool_kg_client import get_concept_context_for_scope

router = APIRouter(prefix="/voice-tests", tags=["voice-tests"])


@router.post("/generate", response_model=GenerateAssessorContentOut)
async def generate(body: GenerateAssessorContentIn) -> GenerateAssessorContentOut:
    """Grounded reference questions + spoken-friendly context for a Voice
    Test, generated once at creation time (see nool-core's
    src/api/routes/voice_test.py:create_test) and handed to the colearner
    pipeline's SessionConfig on every attempt - same shape/reasoning as
    question_paper.py's /question-papers/generate.
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
    result = generate_assessor_content(
        api_key=settings.openai_api_key,
        concept_context=context,
        bloom_levels=body.bloom_levels,
        num_questions=body.num_questions,
    )
    return GenerateAssessorContentOut(**result)
