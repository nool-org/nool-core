from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_db_session, require_role
from src.api.routes.student_dashboard import _learning_streak
from src.api.schemas.gamification import (
    AchievementOut,
    AwardSessionXpIn,
    BadgeOut,
    DailyGoalOut,
    QuestOut,
    StudentGamificationOut,
)
from src.domain.models import (
    Role,
    StudentChapterProgress,
    StudentDailyGoalCompletion,
    StudentGamification,
    StudentTestResult,
    User,
)
from src.services.gamification import BADGES, class_quests, sync_badges

router = APIRouter(prefix="/api/v1", tags=["gamification"])

# Fixed, in-code catalog rather than a DB table - these are app-defined,
# not admin-configurable. Keys are stable identifiers a client can pass
# back as `completedGoalId` on POST /me/gamification/award - matching
# nool-app's mocks/data/studentGamification.ts fixture ids exactly, so
# the same client code marks the same goal complete whether it's talking
# to the mock or this real endpoint.
DAILY_GOAL_CATALOG = [
    {"key": "goal-open-app", "title": "Open the app & check your plan", "xp_reward": 10},
    {"key": "goal-finish-voice-test", "title": "Finish one Voice Test", "xp_reward": 50},
]


async def _get_or_create_state(session: AsyncSession, student_id) -> StudentGamification:
    result = await session.execute(
        select(StudentGamification).where(StudentGamification.student_id == student_id)
    )
    state = result.scalar_one_or_none()
    if state is None:
        state = StudentGamification(student_id=student_id)
        session.add(state)
        await session.flush()
    return state


SESSION_XP = 50  # matches the app's FLAT_SESSION_XP ("Finish for +50 XP")
HINT_XP_COST = 5  # matches the app's HINT_XP_COST


def session_xp(hints_used: int) -> int:
    return max(0, SESSION_XP - max(hints_used, 0) * HINT_XP_COST)


async def apply_xp(session: AsyncSession, student_id, xp_earned: int, goal_key: str | None = None) -> StudentGamification:
    """Adds XP (rolling over levels) and optionally marks a daily goal done.
    Caller commits."""
    state = await _get_or_create_state(session, student_id)
    xp = state.xp + max(xp_earned, 0)
    level = state.level
    while xp >= state.xp_for_next_level:
        xp -= state.xp_for_next_level
        level += 1
    state.xp = xp
    state.level = level
    if goal_key:
        await _mark_goal_complete(session, student_id, goal_key)
    return state


async def _completed_goal_keys_today(session: AsyncSession, student_id) -> set[str]:
    today = datetime.now(timezone.utc).date()
    result = await session.execute(
        select(StudentDailyGoalCompletion.goal_key).where(
            StudentDailyGoalCompletion.student_id == student_id,
            StudentDailyGoalCompletion.completed_date == today,
        )
    )
    return {row[0] for row in result.all()}


async def _mark_goal_complete(session: AsyncSession, student_id, goal_key: str) -> None:
    today = datetime.now(timezone.utc).date()
    existing = await session.execute(
        select(StudentDailyGoalCompletion).where(
            StudentDailyGoalCompletion.student_id == student_id,
            StudentDailyGoalCompletion.goal_key == goal_key,
            StudentDailyGoalCompletion.completed_date == today,
        )
    )
    if existing.scalar_one_or_none() is None:
        session.add(
            StudentDailyGoalCompletion(
                student_id=student_id, goal_key=goal_key, completed_date=today
            )
        )


def _daily_goals_out(completed_keys: set[str]) -> list[DailyGoalOut]:
    goals = []
    for entry in DAILY_GOAL_CATALOG:
        done = entry["key"] in completed_keys
        subtitle = f"Done · +{entry['xp_reward']} XP" if done else f"+{entry['xp_reward']} XP"
        goals.append(
            DailyGoalOut(
                id=entry["key"],
                title=entry["title"],
                subtitle=subtitle,
                xp_reward=entry["xp_reward"],
                completed=done,
            )
        )
    return goals


async def _achievements_out(session: AsyncSession, user: User) -> list[AchievementOut]:
    """Every achievement is derived live from real rows, not a separate
    stored "unlocked" flag - same honesty standard as progress.py's
    next_best_focus.
    """
    streak = await _learning_streak(session, user.id)

    test_count_result = await session.execute(
        select(func.count()).select_from(StudentTestResult).where(
            StudentTestResult.student_id == user.id
        )
    )
    test_count = test_count_result.scalar_one()

    mastery_result = await session.execute(
        select(func.max(StudentTestResult.mastery_percent)).where(
            StudentTestResult.student_id == user.id
        )
    )
    best_mastery = mastery_result.scalar_one() or 0

    chapter_complete_result = await session.execute(
        select(func.count()).select_from(StudentChapterProgress).where(
            StudentChapterProgress.student_id == user.id,
            StudentChapterProgress.completed_at.is_not(None),
        )
    )
    chapters_completed = chapter_complete_result.scalar_one()

    return [
        AchievementOut(
            id="streak-7",
            title="7-day streak",
            icon_key="flame",
            unlocked=streak.current_streak_days >= 7,
        ),
        AchievementOut(
            id="voice-tests-10", title="10 Voice Tests", icon_key="mic", unlocked=test_count >= 10
        ),
        AchievementOut(
            id="mastery-90", title="90% Club", icon_key="medal", unlocked=best_mastery >= 90
        ),
        AchievementOut(
            id="first-chapter-complete",
            title="First Chapter Complete",
            icon_key="target",
            unlocked=chapters_completed >= 1,
        ),
    ]


async def _gamification_out(session: AsyncSession, user: User, state: StudentGamification) -> StudentGamificationOut:
    """Commits pending writes (XP, goals, newly earned badges), then builds
    the response."""
    earned = await sync_badges(session, user.id)
    await session.commit()

    completed_keys = await _completed_goal_keys_today(session, user.id)
    achievements = await _achievements_out(session, user)

    return StudentGamificationOut(
        level=state.level,
        xp=state.xp,
        xp_for_next_level=state.xp_for_next_level,
        daily_goals=_daily_goals_out(completed_keys),
        achievements=achievements,
        badges=[
            BadgeOut(id=badge_id, name=name, bloom_level=level, earned=badge_id in earned,
                     earned_at=earned.get(badge_id))
            for badge_id, name, level in BADGES
        ],
        quests=[QuestOut(**quest) for quest in await class_quests(session, user.id)],
    )


@router.get("/me/gamification")
async def get_my_gamification(
    user: User = Depends(require_role(Role.STUDENT)),
    session: AsyncSession = Depends(get_db_session),
) -> StudentGamificationOut:
    state = await _get_or_create_state(session, user.id)
    # Reaching this endpoint at all means the app was opened today.
    await _mark_goal_complete(session, user.id, "goal-open-app")
    return await _gamification_out(session, user, state)


@router.post("/me/gamification/award", summary="Award XP for a completed activity")
async def award_session_xp(
    body: AwardSessionXpIn,
    user: User = Depends(require_role(Role.STUDENT)),
    session: AsyncSession = Depends(get_db_session),
) -> StudentGamificationOut:
    # XP is the server's to give (V-021): the client names a goal it reached, never an amount. Only
    # opening the app is a client-side event; the others are marked where they happen (ai_assessor).
    reward = 0
    if body.completed_goal_id == "goal-open-app" and body.completed_goal_id not in await _completed_goal_keys_today(
            session, user.id):
        reward = next(g["xp_reward"] for g in DAILY_GOAL_CATALOG if g["key"] == body.completed_goal_id)
    state = await apply_xp(session, user.id, reward, body.completed_goal_id if reward else None)
    return await _gamification_out(session, user, state)
