from datetime import datetime
from typing import Literal

from src.domain.models import BloomLevel

from .common import CamelModel


class DailyGoalOut(CamelModel):
    id: str
    title: str
    subtitle: str
    xp_reward: int
    completed: bool


class AchievementOut(CamelModel):
    id: str
    title: str
    icon_key: str
    unlocked: bool


class BadgeOut(CamelModel):
    id: str
    name: str
    bloom_level: BloomLevel
    earned: bool
    earned_at: datetime | None


class QuestOut(CamelModel):
    id: str
    title: str
    progress: int
    target: int
    scope: Literal["class"] = "class"


class StudentGamificationOut(CamelModel):
    level: int
    xp: int
    xp_for_next_level: int
    daily_goals: list[DailyGoalOut]
    achievements: list[AchievementOut]
    badges: list[BadgeOut]
    quests: list[QuestOut]


class AwardSessionXpIn(CamelModel):
    # no amount: the server decides (an old client's xpEarned is ignored)
    completed_goal_id: str | None = None
