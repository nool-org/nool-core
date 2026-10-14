import logging
import time
import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.errors import NotFoundError, ValidationError

from src.api.deps import get_db_session, require_feature, require_role
from src.api.schemas.assistant import ChatMessageOut, SendMessageIn, TeacherSummaryOut
from src.api.schemas.common import ListEnvelope
from src.domain.models import AssistantMessage, ChatRole, Feature, Role, User
from src.services import generation_client
from src.services.teacher_context import build_teacher_facts

router = APIRouter(prefix="/api/v1", tags=["assistant"])
logger = logging.getLogger(__name__)

_GREETING = (
    "Hi, I'm your Nool assistant. Ask me about Voice Tests, Homework, Question Papers, "
    "or a class's progress."
)

# Fallback only (generation service down): fixed how-to answers by keyword.
# Normal replies come from generation's teacher-assistant over teacher_context facts.
_RULES: list[tuple[str, str]] = [
    ("voice test", "To create a Voice Test: Home → Create → Voice Test, then pick a class, subject, chapter, topic and Bloom levels, and schedule it."),
    ("homework", "Homework is generated from a Test's diagnostic gap once results are ready - open the Test's results and choose Generate Homework."),
    ("question paper", "Question Papers are built independently of Voice Tests - go to Question Papers → New, configure the blueprint, then Generate."),
    ("progress", "Check a class's progress from the Improvement tab on a completed Test, or the class-level analytics on your dashboard."),
]


def _reply_for(text: str) -> str:
    lowered = text.lower()
    for keyword, reply in _RULES:
        if keyword in lowered:
            return reply
    return "I'm not sure about that yet - try asking about Voice Tests, Homework, Question Papers, or a class's progress."


async def _ensure_greeting(session: AsyncSession, teacher_id) -> None:
    existing = await session.execute(
        select(AssistantMessage.id).where(AssistantMessage.teacher_id == teacher_id).limit(1)
    )
    if existing.scalar_one_or_none() is None:
        session.add(AssistantMessage(teacher_id=teacher_id, role=ChatRole.ASSISTANT, text=_GREETING))
        await session.commit()


@router.get("/assistant/messages")
async def list_messages(
    user: User = Depends(require_role(Role.TEACHER)),
    _feature: User = Depends(require_feature(Feature.ASSISTANT)),
    session: AsyncSession = Depends(get_db_session),
) -> ListEnvelope[ChatMessageOut]:
    """The full thread, oldest first. Always has at least one message - a
    fixed greeting seeds a new thread.
    """
    await _ensure_greeting(session, user.id)
    result = await session.execute(
        select(AssistantMessage)
        .where(AssistantMessage.teacher_id == user.id)
        .order_by(AssistantMessage.created_at)
    )
    items = [
        ChatMessageOut(id=str(m.id), role=m.role, text=m.text, created_at=m.created_at)
        for m in result.scalars().all()
    ]
    return ListEnvelope(items=items, total=len(items))


@router.post("/assistant/messages", status_code=201, summary="Send a message")
async def send_message(
    body: SendMessageIn,
    user: User = Depends(require_role(Role.TEACHER)),
    _feature: User = Depends(require_feature(Feature.ASSISTANT)),
    session: AsyncSession = Depends(get_db_session),
) -> ChatMessageOut:
    """Appends the teacher's message to the thread server-side, then
    returns only the assistant's reply - not the full thread - so the
    client appends both turns locally instead of re-fetching.
    """
    text = body.text.strip()
    if not text:
        raise ValidationError("text is empty after trimming.")

    await _ensure_greeting(session, user.id)
    history = (await session.execute(
        select(AssistantMessage).where(AssistantMessage.teacher_id == user.id)
        .order_by(AssistantMessage.created_at.desc()).limit(12)
    )).scalars().all()
    session.add(AssistantMessage(teacher_id=user.id, role=ChatRole.USER, text=text))
    try:
        # real answers, grounded in this teacher's own classes, tests and homework - only the
        # challenge they picked, when they picked one
        scope = {}
        for key, value in (("voice_test_id", body.test_id), ("quiz_test_id", body.quiz_test_id)):
            if value:
                try:
                    scope[key] = uuid.UUID(value)
                except ValueError:
                    raise ValidationError("Unknown challenge.")
        facts = await build_teacher_facts(session, user, **scope)
        question = text
        if body.focus_label:
            facts["teacher_is_asking_about"] = body.focus_label
            question = f"(About {body.focus_label}) {text}"
        answer = await generation_client.assistant_reply(
            facts,
            [{"role": "teacher" if m.role == ChatRole.USER else "assistant", "text": m.text} for m in reversed(history)],
            question,
        )
    except generation_client.GenerationServiceUnavailableError:
        logger.warning("assistant_llm_unavailable")
        answer = _reply_for(text)  # how-to answers still work without the model
    reply = AssistantMessage(teacher_id=user.id, role=ChatRole.ASSISTANT, text=answer)
    session.add(reply)
    await session.commit()
    await session.refresh(reply)
    return ChatMessageOut(id=str(reply.id), role=reply.role, text=reply.text, created_at=reply.created_at)


SUMMARY_TTL_SECONDS = 15 * 60
# ponytail: per-process cache (core can run several replicas - each just regenerates); move to redis/DB if cost shows up
_summaries: dict[str, tuple[float, list[str]]] = {}


@router.get("/teacher/summary", summary="AI summary of what needs attention")
async def teacher_summary(
    refresh: bool = False,
    user: User = Depends(require_role(Role.TEACHER)),
    _feature: User = Depends(require_feature(Feature.ASSISTANT)),
    session: AsyncSession = Depends(get_db_session),
) -> TeacherSummaryOut:
    """2-3 bullets for the teacher Home, from the same facts the assistant
    uses. Cached 15 min per teacher; empty bullets when the model is down
    (the Home simply hides the card)."""
    key = str(user.id)
    cached = _summaries.get(key)
    if cached and not refresh and time.monotonic() - cached[0] < SUMMARY_TTL_SECONDS:
        return TeacherSummaryOut(bullets=cached[1])
    try:
        bullets = await generation_client.teacher_summary(await build_teacher_facts(session, user))
        # "No reviews waiting" tells the teacher nothing - the card hides when there's nothing to say
        bullets = [b for b in bullets if not b.strip().lower().startswith(("no ", "there are no", "nothing "))]
    except generation_client.GenerationServiceUnavailableError:
        logger.warning("teacher_summary_llm_unavailable")
        return TeacherSummaryOut(bullets=[])
    _summaries[key] = (time.monotonic(), bullets)
    return TeacherSummaryOut(bullets=bullets)


@router.get("/teacher/summary/challenge", summary="AI summary of one challenge's results")
async def challenge_summary(
    voice_test_id: str | None = Query(default=None, alias="testId"),
    quiz_test_id: str | None = Query(default=None, alias="quizTestId"),
    user: User = Depends(require_role(Role.TEACHER)),
    _feature: User = Depends(require_feature(Feature.ASSISTANT)),
    session: AsyncSession = Depends(get_db_session),
) -> TeacherSummaryOut:
    """2-3 bullets on one Speak with Milo or Fix Milo challenge - how the
    class did, who needs help, what to do next - shown on its results page.
    Only the teacher's own challenges (the facts are scoped like GET /tests).
    Cached 15 min per challenge; empty when there's nothing yet or the model is down."""
    if (voice_test_id is None) == (quiz_test_id is None):
        raise ValidationError("Give exactly one of testId or quizTestId.")
    try:
        target = uuid.UUID(voice_test_id or quiz_test_id)
    except ValueError:
        raise NotFoundError("No such challenge.")
    key = f"{user.id}:{target}"
    cached = _summaries.get(key)
    if cached and time.monotonic() - cached[0] < SUMMARY_TTL_SECONDS:
        return TeacherSummaryOut(bullets=cached[1])
    facts = await build_teacher_facts(session, user, voice_test_id=target if voice_test_id else None,
                                      quiz_test_id=target if quiz_test_id else None)
    found = facts["recent_voice_tests"] or facts["open_challenges"]
    if not found:
        raise NotFoundError("No such challenge.")
    if not any(t.get("class_average_percent") is not None for t in found):
        return TeacherSummaryOut(bullets=[])  # no results yet - nothing worth a model call
    try:
        bullets = await generation_client.teacher_summary(facts)
    except generation_client.GenerationServiceUnavailableError:
        logger.warning("challenge_summary_llm_unavailable")
        return TeacherSummaryOut(bullets=[])
    _summaries[key] = (time.monotonic(), bullets)
    return TeacherSummaryOut(bullets=bullets)
