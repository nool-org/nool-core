"""Bloom/concept growth from a voice test to its follow-up MCQ test.

before = the voice-test score for a Bloom level / concept; after = the MCQ
score corrected for guessing on 4 options: (p - 25) / 75 * 100, floor 0 -
so a student guessing blindly (25%) scores 0, not 25.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.domain.models import McqTest, User, VoiceTest

FLAG_BELOW = 60  # re-check score under this = not mastered yet (challenge_report's STRUGGLING_BELOW)
MIN_ASKED = 2  # V-008: a Bloom level the re-check asked once is 'Not assessed', not a one-answer swing


def corrected(percent: int) -> int:
    return max(0, round((percent - 25) / 75 * 100))


def percent(correct: int, total: int) -> int:
    return round(100 * correct / total) if total else 0


def growth_entries(kind: str, after_raw: dict[str, int], before: dict[str, int],
                   asked: dict[str, int] | None = None) -> list[dict]:
    """One entry per key measured on the MCQ test that also has a voice-test score;
    with `asked` (questions per key), only keys asked at least MIN_ASKED times."""
    out = []
    for key, raw in after_raw.items():
        if before.get(key) is None or (asked is not None and asked.get(key, 0) < MIN_ASKED):
            continue
        after = corrected(raw)
        out.append({"key": key, "kind": kind, "before": before[key], "afterCorrected": after,
                    "delta": after - before[key]})
    return out


def _averaged(entries: list[dict]) -> list[dict]:
    by_key: dict[str, list[dict]] = {}
    for e in entries:
        by_key.setdefault(e["key"], []).append(e)
    out = []
    for key, rows in by_key.items():
        before = round(sum(r["before"] for r in rows) / len(rows))
        after = round(sum(r["afterCorrected"] for r in rows) / len(rows))
        out.append({"key": key, "before": before, "after": after, "delta": after - before})
    return out


async def class_growth(session: AsyncSession, test: VoiceTest) -> dict:
    """Every completed MCQ test of this voice test: per student, and the
    class average per Bloom level and per concept (over the students
    measured on it)."""
    rows = (await session.execute(
        select(McqTest, User.display_name).join(User, User.id == McqTest.student_id)
        .where(McqTest.voice_test_id == test.id, McqTest.completed_at.is_not(None))
        .order_by(User.display_name)
    )).all()
    students, entries = [], []
    for mcq, name in rows:
        result = mcq.result or {}
        students.append({"student_id": str(mcq.student_id), "name": name, "before": result.get("before"),
                         "after": result.get("after"),
                         "delta": result["after"] - result["before"] if result.get("before") is not None else None,
                         "flagged": result.get("after") is not None and result["after"] < FLAG_BELOW})
        entries.extend(result.get("growth") or [])
    bloom = _averaged([e for e in entries if e["kind"] == "bloom"])
    concepts = _averaged([e for e in entries if e["kind"] == "concept"])
    return {
        "test_id": str(test.id),
        "students": students,
        "per_bloom": [{"level": e["key"], **{k: e[k] for k in ("before", "after", "delta")}} for e in bloom],
        "concepts": [{"concept": e["key"], **{k: e[k] for k in ("before", "after", "delta")}} for e in concepts],
    }
