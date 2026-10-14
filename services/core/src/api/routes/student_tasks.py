"""The student's Tests tab - every assigned task with its steps
(src/services/student_tasks.py)."""

from fastapi import APIRouter, BackgroundTasks, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_db_session, require_role
from src.api.schemas.student_tasks import TaskOut, TasksOut
from src.domain.models import Role, User
from src.services import student_tasks, topic_quiz

router = APIRouter(prefix="/api/v1", tags=["student-tasks"])


@router.get("/me/tasks")
async def get_my_tasks(
    background_tasks: BackgroundTasks,
    user: User = Depends(require_role(Role.STUDENT)),
    session: AsyncSession = Depends(get_db_session),
) -> TasksOut:
    tasks = await student_tasks.tasks_for(session, user)
    await session.commit()  # MCQ checks may have just been created/unlocked
    # any checkpoint that should be ready but isn't gets built now, after the response
    background_tasks.add_task(topic_quiz.heal, user.id)
    return TasksOut(tasks=[TaskOut.model_validate(t) for t in tasks])
