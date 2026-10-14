from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_db_session, require_feature, require_role
from src.api.schemas.leaderboard import ClassLeaderboardOut, LeaderboardEntryOut
from src.domain.models import Feature, Role, SchoolClass, StudentPoints, StudentProfile, User

router = APIRouter(prefix="/api/v1", tags=["leaderboard"])


def _initials(name: str) -> str:
    parts = name.split()
    return "".join(p[0].upper() for p in parts[:2]) if parts else ""


@router.get("/me/leaderboard")
async def get_my_leaderboard(
    user: User = Depends(require_role(Role.STUDENT)),
    _feature: User = Depends(require_feature(Feature.LEADERBOARD)),
    session: AsyncSession = Depends(get_db_session),
) -> ClassLeaderboardOut:
    profile_result = await session.execute(
        select(StudentProfile).where(StudentProfile.user_id == user.id)
    )
    profile = profile_result.scalar_one_or_none()
    if profile is None:
        return ClassLeaderboardOut(class_label="", entries=[])

    class_result = await session.execute(select(SchoolClass).where(SchoolClass.id == profile.class_id))
    school_class = class_result.scalar_one_or_none()
    class_label = f"Class {school_class.grade}{school_class.section}" if school_class else ""

    rank = func.rank().over(order_by=StudentPoints.points.desc())
    rows = await session.execute(
        select(User.id, User.display_name, StudentPoints.points, rank.label("rank"), User.avatar)
        .join(StudentProfile, StudentProfile.user_id == User.id)
        .join(StudentPoints, StudentPoints.student_id == User.id, isouter=True)
        .where(StudentProfile.class_id == profile.class_id)
        .order_by(StudentPoints.points.desc().nulls_last())
    )
    entries = [
        LeaderboardEntryOut(
            student_id=str(student_id),
            display_name=display_name,
            initials=_initials(display_name),
            avatar=avatar,
            points=points or 0,
            rank=rank_value,
            is_current_student=student_id == user.id,
        )
        for student_id, display_name, points, rank_value, avatar in rows.all()
    ]
    return ClassLeaderboardOut(class_label=class_label, entries=entries)
