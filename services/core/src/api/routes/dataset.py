from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_db_session, require_role
from src.api.schemas.common import ListEnvelope
from src.api.schemas.dataset import DatasetOut
from src.domain.models import Role, User
from src.repositories.dataset_access import visible_datasets

router = APIRouter(prefix="/api/v1", tags=["datasets"])


@router.get("/datasets")
async def list_datasets(
    subject_id: Annotated[str | None, Query(alias="subjectId")] = None,
    grade: int | None = None,
    user: User = Depends(require_role(Role.TEACHER)),
    session: AsyncSession = Depends(get_db_session),
) -> ListEnvelope[DatasetOut]:
    """The caller's visible datasets - see repositories/dataset_access.py
    for the rule (school config + class scope). `subjectId`/`grade`
    optionally narrow to one class context, e.g. the Homework/Question
    Paper being configured.
    """
    items = [
        DatasetOut(
            id=str(d.id), name=d.name, question_count=d.question_count, description=d.description,
            type=d.type,
        )
        for d in await visible_datasets(session, user, subject_id=subject_id, grade=grade)
    ]
    return ListEnvelope(items=items, total=len(items))
