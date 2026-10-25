"""What a teacher could set for a chapter, and why - a suggestion, never a
decision: the teacher takes it from there (GET /classes/{id}/chapters/{id}/challenge-suggestion).

Rules, no model calls. Three inputs:
- which of the chapter's topics are worked on paper (topic_work): the share of
  the topic's bank questions whose answers hold a chemical equation or a
  calculation. Balancing an equation or working a circuit is something to
  see and fix, not say - those topics suit Fix Milo's homework; the rest are
  explained, which suits Speak with Milo;
- the chapter's nature (NATURE, tagged by hand once) - the levels to ask at;
- how the class is doing on it so far: its topic checkpoints
  (topic_quiz.class_topic_summary) - weakest topics, weakest thinking skill,
  students still stuck after the re-check.

So: no worked topics -> Speak with Milo; mostly worked -> Fix Milo's
homework; a mix -> Speak with Milo with Fix Milo cards (Milo puts a worked
equation or sum on screen mid-talk; the student taps the slip, fixes it, says
why). Only formats the subject offers (subject_rules) are suggested, and not
one the class already has on this chapter (challenge_rules).
"""

import re
import uuid

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.domain.models import BloomLevel, Chapter, DatasetQuestion, Subject, Topic
from src.services import challenge_rules, daily_practice, subject_rules, topic_quiz

# what kind of chapter it is; a Maths chapter is "problems" unless listed
NATURE: dict[str, str] = {
    "Chemical Reactions and Equations": "equations",
    "Acids, Bases and Salts": "explain",
    "Metals and Non-metals": "explain",
    "Carbon and its Compounds": "explain",
    "Life Processes": "process",
    "Control and Coordination": "process",
    "How do Organisms Reproduce?": "process",
    "Heredity": "explain",
    "Light – Reflection and Refraction": "numerical",
    "The Human Eye and the Colourful World": "explain",
    "Electricity": "numerical",
    "Magnetic Effects of Electric Current": "rules",
    "Our Environment": "facts",
}
NATURE_TEXT = {
    "numerical": "mostly calculations", "equations": "built on chemical equations",
    "explain": "about explaining why things happen", "process": "about processes that happen in order",
    "rules": "about rules and directions", "facts": "mostly facts and terms", "problems": "problem solving",
}
B = BloomLevel
LEVELS = {
    "numerical": (B.UNDERSTAND, B.APPLY, B.ANALYZE), "equations": (B.REMEMBER, B.UNDERSTAND, B.APPLY),
    "problems": (B.UNDERSTAND, B.APPLY, B.ANALYZE), "explain": (B.UNDERSTAND, B.APPLY, B.ANALYZE),
    "process": (B.REMEMBER, B.UNDERSTAND, B.ANALYZE), "rules": (B.UNDERSTAND, B.APPLY),
    "facts": (B.REMEMBER, B.UNDERSTAND, B.APPLY),
}
WEAK_BELOW = 70  # a topic's checkpoint average under this is worth focusing on
MAX_LEVELS = 4
# a chemical equation (arrows, also as {tex}\rightarrow) / a calculation (2 x 3, 4 = 5) in an answer
EQUATION = r"(→|->|⟶|rightarrow)"
CALCULATION = r"[0-9]+(\.[0-9]+)? ?[×x*/=+−-] ?[0-9]"
# ponytail: share of bank answers with an equation or sum; tuned on Class 10 Science (balancing 71%,
# Electricity 12-54%, Light 13%, biology/magnetism <5%) - a tagged topic list if a subject fools it
WORKED_SHARE = 0.12
MIN_BANK = 10  # fewer bank questions than this says nothing about a topic
MAX_CARDS = 1  # voice_test_prep.MAX_CARDS - a card takes ~2 of the 8 minutes
MOSTLY = 0.8  # more than this share of topics worked (Electricity) -> Fix Milo leads


def nature_of(subject: str | None, chapter: str) -> str:
    return NATURE.get(chapter, "problems" if subject == "Mathematics" else "explain")


async def topic_work(session: AsyncSession, chapter_id: uuid.UUID) -> list[dict]:
    """Per topic of the chapter with enough bank questions, in book order:
    {topic_id, name, equations, calculations} - shares of its answers."""
    text = func.coalesce(DatasetQuestion.solution, "") + " " + func.coalesce(DatasetQuestion.answer, "")
    share = lambda pattern: func.avg(case((text.op("~")(pattern), 1.0), else_=0.0))  # noqa: E731
    rows = (await session.execute(
        select(Topic.id, Topic.name, func.count(DatasetQuestion.id), share(EQUATION), share(CALCULATION))
        .join(DatasetQuestion, DatasetQuestion.topic_id == Topic.id)
        .where(Topic.chapter_id == chapter_id).group_by(Topic.id, Topic.name)
    )).all()
    return sorted(({"topic_id": str(t), "name": daily_practice.tidy(name), "equations": float(eq or 0),
                    "calculations": float(calc or 0)} for t, name, n, eq, calc in rows if n >= MIN_BANK),
                  key=lambda w: _book_order(w["name"]))


def _book_order(name: str) -> list:
    """"1.2 ..." before "1.10 ..." (voice_test_prep._textbook_order)."""
    return [(0, int(p), "") if p.isdigit() else (1, 0, p.lower()) for p in re.split(r"(\d+)", name)]


def _names(topics: list[dict]) -> str:
    """Topics by their number when they have one ("4.3, 4.4") - short enough for a phone."""
    return ", ".join(t["name"].split(" ")[0] if re.match(r"\d+\.\d+ ", t["name"]) else t["name"] for t in topics)


def decide(subject: str | None, chapter: str, topics: list[dict], taken: set[str], work: list[dict]) -> dict:
    """The suggestion from the chapter, its topic checkpoint rows (class_topic_summary),
    the formats the class already has on it and its topics' work (topic_work)."""
    nature = nature_of(subject, chapter)
    offered = subject_rules.challenge_formats(subject)

    quizzed = [t for t in topics if t["quizzed"]]
    average = {t["topic_id"]: t["average_percent"] for t in quizzed}
    weak = sorted((t for t in quizzed if (t["average_percent"] or 0) < WEAK_BELOW),
                  key=lambda t: t["average_percent"] or 0)[:3]
    # the class's skill averages across topics, weighted by how many took each checkpoint
    totals: dict[str, list[float]] = {}
    for t in quizzed:
        for level, pct in (t["per_bloom"] or {}).items():
            acc = totals.setdefault(level, [0.0, 0.0])
            acc[0] += pct * t["quizzed"]
            acc[1] += t["quizzed"]
    skills = {level: round(s / n) for level, (s, n) in totals.items() if n}
    weakest = min(skills, key=skills.get) if skills else None
    stuck = sum(len(t["needs_teacher"]) for t in topics)

    # worked topics, the class's weakest first (untested ones after, in book order)
    worked = sorted((w for w in work if w["equations"] + w["calculations"] >= WORKED_SHARE),
                    key=lambda w: average.get(w["topic_id"]) if average.get(w["topic_id"]) is not None else 101)
    what = ("chemical equations" if sum(w["equations"] for w in worked) >= sum(w["calculations"] for w in worked)
            else "calculations")
    one = "an equation" if what == "chemical equations" else "a sum"
    lead = chapter if not work else f"{chapter} is {NATURE_TEXT[nature]}"
    fix = {"format": "fix_steps", "label": "Fix Milo's homework", "fix_cards": 0,
           "topics": [{"topic_id": w["topic_id"], "name": w["name"]} for w in worked],
           "why": (f"Worked on paper ({what}): students find the step Milo slipped on and fix it - the way marks "
                   "are lost in exams." if worked else
                   "Milo writes out the steps and slips once - students find the slip and fix it.")}
    voice = {"format": "voice", "label": "Speak with Milo", "fix_cards": 0, "topics": [],
             "why": f"{lead}: students explain it out loud, so gaps in understanding show up."}
    if "voice" not in offered:
        ranked = [fix]
    elif not worked:
        ranked = [voice, fix]
    else:
        cards = {**voice, "label": "Speak with Milo + Fix Milo cards", "fix_cards": min(MAX_CARDS, len(worked)),
                 "topics": fix["topics"][:MAX_CARDS],
                 "why": f"Milo talks the ideas through, then shows {one} with one slip on screen - students tap it, "
                        "fix it and say why."}
        ranked = [fix, cards] if len(worked) > MOSTLY * len(work) else [cards, fix]
    options = [o for o in ranked if o["format"] not in taken]

    levels = list(LEVELS[nature])
    if weakest and weakest in B.__members__ and B(weakest) not in levels:
        levels.append(B(weakest))
    levels = sorted(set(levels), key=list(B).index)[:MAX_LEVELS]

    evidence = []
    if worked and "voice" in offered:
        explained = _names([w for w in work if w not in worked])
        evidence.append(f"Worked on paper: {_names(worked)}." + (f" Explained in words: {explained}." if explained else ""))
    if weakest:
        takers = max(t["quizzed"] for t in quizzed)
        evidence.append(f"Your class is weakest at {weakest.title()} ({skills[weakest]}%) in this chapter's checkpoints"
                        f" - {takers} student{'s' if takers != 1 else ''} so far.")
    if weak:
        evidence.append("Weakest topics: " + ", ".join(
            f"{daily_practice.tidy(t['topic_name'])} ({t['average_percent']}%)" for t in weak) + ".")
    if stuck:
        evidence.append(f"{stuck} student{'s' if stuck != 1 else ''} still stuck after the re-check.")
    if taken and options:
        evidence.append("Already set on this chapter: " + ", ".join(
            "Speak with Milo" if f == "voice" else "Fix Milo's homework" for f in sorted(taken)) + ".")
    if not topics:
        note = "No topic of this chapter is marked taught yet - the suggestion comes from the chapter alone."
    elif not quizzed:
        note = "No checkpoint results yet - the suggestion comes from the chapter alone."
    else:
        note = None

    return {
        "nature": NATURE_TEXT[nature],
        "taken": bool(taken) and not options,
        "options": options,
        "bloom_levels": [level.value for level in levels],
        "focus_topics": [{"topic_id": t["topic_id"], "name": daily_practice.tidy(t["topic_name"]),
                          "average_percent": t["average_percent"]} for t in weak],
        "evidence": evidence,
        "note": note,
    }


async def suggest(session: AsyncSession, class_id: uuid.UUID, chapter: Chapter) -> dict:
    subject = await session.get(Subject, chapter.subject_id)
    topics = await topic_quiz.class_topic_summary(session, class_id, chapter.id)
    taken = await challenge_rules.taken_formats(session, class_id, chapter.id)
    return decide(subject.name if subject else None, chapter.name, topics, taken,
                  await topic_work(session, chapter.id))
