"""Live rules check (C10): the paper's questions against its blueprint.

    await rules_check(session, paper) -> RulesCheckOut   (W5-02 imports this)

Percent mixes (Bloom, difficulty) are shares of MARKS, the way a board
paper is weighed. A mix more than MIX_TOLERANCE points off its target is a
warning; marks/section mismatches are errors (ok = no errors).

Estimated writing time per question (MINUTES_* below):
    MCQ           1.5 min flat
    TRUE_FALSE    1   min flat
    SHORT_ANSWER  2   min per mark
    LONG_ANSWER   2.5 min per mark
    unknown type  2   min per mark
Total is rounded up; over the blueprint's durationMinutes is a warning.
"""

import math
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer_group

from src.api.schemas.question_paper import (
    BloomMixOut,
    ChapterCoverageOut,
    DifficultyMix,
    MarksTargetOut,
    RulesCheckOut,
    RulesIssueOut,
    SectionCheckOut,
)
from src.domain.models import (
    BloomLevel,
    Chapter,
    PaperDifficultyLevel,
    QuestionPaper,
    QuestionPaperBloomDistribution,
    QuestionPaperChapter,
    QuestionPaperDifficultyDistribution,
    QuestionPaperSection,
    QuestionPaperValidation,
    QuestionType,
)
from src.domain.models.question_paper import COPILOT_COLUMNS, QuestionPaperQuestion
from src.services.paper_repeats import find_repeats
from src.services.question_selector import crowded, duplicates_of, over_marked, plain_recall

MIX_TOLERANCE = 10
MINUTES_FLAT = {QuestionType.MCQ: 1.5, QuestionType.TRUE_FALSE: 1.0}
MINUTES_PER_MARK = {QuestionType.SHORT_ANSWER: 2.0, QuestionType.LONG_ANSWER: 2.5}
MINUTES_PER_MARK_DEFAULT = 2.0


def estimate_minutes(questions) -> int:
    total = 0.0
    for q in questions:
        if q.question_type in MINUTES_FLAT:
            total += MINUTES_FLAT[q.question_type]
        else:
            total += MINUTES_PER_MARK.get(q.question_type, MINUTES_PER_MARK_DEFAULT) * q.marks
    return math.ceil(total)


def _percent(part: int, whole: int) -> int:
    return round(100 * part / whole) if whole else 0


async def rules_check(session: AsyncSession, paper: QuestionPaper) -> RulesCheckOut:
    questions = list((await session.execute(
        select(QuestionPaperQuestion).options(undefer_group(COPILOT_COLUMNS))
        .where(QuestionPaperQuestion.paper_id == paper.id).order_by(QuestionPaperQuestion.order)
    )).scalars().all())
    sections = (await session.execute(
        select(QuestionPaperSection).where(QuestionPaperSection.paper_id == paper.id)
    )).scalars().all()
    bloom_targets = {d.bloom_level: d.value for d in (await session.execute(
        select(QuestionPaperBloomDistribution).where(QuestionPaperBloomDistribution.paper_id == paper.id)
    )).scalars().all()}
    difficulty_targets = {d.level: d.value for d in (await session.execute(
        select(QuestionPaperDifficultyDistribution).where(QuestionPaperDifficultyDistribution.paper_id == paper.id)
    )).scalars().all()}
    blueprint_chapters = list((await session.execute(
        select(QuestionPaperChapter.chapter_id).where(QuestionPaperChapter.paper_id == paper.id)
    )).scalars().all())

    issues: list[RulesIssueOut] = []

    def issue(code, severity, message, question_id=None):
        issues.append(RulesIssueOut(code=code, severity=severity, message=message,
                                    question_id=str(question_id) if question_id else None))

    total = sum(q.marks for q in questions)
    if not questions:
        issue("no_questions", "error", "The paper has no questions.")
    if paper.total_marks is not None and total != paper.total_marks:
        issue("total_marks_mismatch", "error", f"Questions add up to {total} marks, the blueprint says {paper.total_marks}.")

    # sections
    section_out = []
    for s in sections:
        mine = [q for q in questions if q.section_name == s.label]
        actual_marks = sum(q.marks for q in mine)
        target_marks = s.question_count * s.marks_each
        ok = actual_marks == target_marks and len(mine) == s.question_count
        section_out.append(SectionCheckOut(name=s.label, target_marks=target_marks, actual_marks=actual_marks,
                                           target_count=s.question_count, actual_count=len(mine), ok=ok))
        if not ok:
            issue("section_mismatch", "error", f"Section {s.label}: {len(mine)} questions / {actual_marks} marks, "
                  f"blueprint wants {s.question_count} / {target_marks}.")
    labels = {s.label for s in sections}
    for q in questions:
        if labels and q.section_name not in labels:
            issue("unsectioned_question", "warning", "This question isn't in any blueprint section.", q.id)

    # Bloom mix (share of marks)
    bloom_marks = defaultdict(int)
    for q in questions:
        bloom_marks[q.bloom_level] += q.marks
    bloom_out = []
    for level in BloomLevel:
        if level not in bloom_targets and level not in bloom_marks:
            continue
        actual = _percent(bloom_marks[level], total)
        target = bloom_targets.get(level)
        bloom_out.append(BloomMixOut(level=level, target_percent=target, actual_percent=actual))
        if bloom_targets and abs(actual - (target or 0)) > MIX_TOLERANCE:
            issue("bloom_off_target", "warning", f"{level.value.title()} is {actual}% of marks, target {target or 0}%.")

    # chapter coverage
    chapter_ids = list(dict.fromkeys([*blueprint_chapters, *(q.chapter_id for q in questions if q.chapter_id)]))
    names = {c.id: c.name for c in (await session.execute(
        select(Chapter).where(Chapter.id.in_(chapter_ids)))).scalars().all()} if chapter_ids else {}
    chapters_out = []
    for chapter_id in chapter_ids:
        mine = [q for q in questions if q.chapter_id == chapter_id]
        chapters_out.append(ChapterCoverageOut(chapter_id=str(chapter_id), name=names.get(chapter_id, ""),
                                               question_count=len(mine), marks=sum(q.marks for q in mine)))
        if not mine and chapter_id in blueprint_chapters:
            issue("chapter_uncovered", "warning", f"No question from {names.get(chapter_id, 'a chosen chapter')}.")

    # difficulty mix (share of marks of questions that have a difficulty)
    rated = [q for q in questions if q.difficulty is not None]
    rated_total = sum(q.marks for q in rated)
    actual_difficulty = {lvl: _percent(sum(q.marks for q in rated if q.difficulty == lvl), rated_total)
                         for lvl in PaperDifficultyLevel}
    if difficulty_targets and rated:
        for lvl in PaperDifficultyLevel:
            target = difficulty_targets.get(lvl, 0)
            if abs(actual_difficulty[lvl] - target) > MIX_TOLERANCE:
                issue("difficulty_off_target", "warning",
                      f"{lvl.value.title()} questions are {actual_difficulty[lvl]}% of marks, target {target}%.")

    estimated = estimate_minutes(questions)
    if paper.duration_minutes is not None and estimated > paper.duration_minutes:
        issue("over_duration", "warning",
              f"Estimated {estimated} minutes to write, the exam is {paper.duration_minutes}.")

    # T-070: the same question worded twice on the paper (same answer and concept), and recall labelled Evaluate/Create
    for later, earlier in duplicates_of([(q.text, q.options, q.answer) for q in questions]).items():
        issue("duplicate", "warning", f"Q{later + 1} repeats Q{earlier + 1}.", questions[later].id)
    for n, q in enumerate(questions):
        if q.bloom_level in (BloomLevel.EVALUATE, BloomLevel.CREATE) and plain_recall(q.text, q.marks):
            issue("level_mismatch", "warning", f"Q{n + 1} is labelled {q.bloom_level.value.title()} but only asks for a fact.", q.id)

    # T-070: one idea used more than twice, a recall question worth 3+ marks, no Hard question on a real paper
    for n, word in crowded([q.text for q in questions]).items():
        issue("repeated_concept", "warning", f'Q{n + 1} is the 3rd+ question about "{word}".', questions[n].id)
    for n, q in enumerate(questions):
        if over_marked(q.text, q.marks, q.bloom_level):
            issue("over_marked", "warning", f"Q{n + 1} is a Remember question worth {q.marks} marks.", q.id)
    if len(rated) >= 8 and not any(q.difficulty == PaperDifficultyLevel.HARD for q in rated):
        issue("no_hard", "warning", "The paper has no Hard question.")

    # C-020: figure questions were asked for but this school's banks hold none (generation noted it on the paper)
    validation = (await session.execute(
        select(QuestionPaperValidation).where(QuestionPaperValidation.paper_id == paper.id))).scalar_one_or_none()
    for text in validation.issues or [] if validation else []:
        if "question banks" in text and "diagrams" in text:
            issue("no_figure_questions", "warning", text)

    repeats = await find_repeats(session, paper, questions)
    for r in repeats:
        issue("repeat", "warning", f'Already used in "{r.matched_title}".', r.question_id)

    return RulesCheckOut(
        ok=not any(i.severity == "error" for i in issues),
        total_marks=MarksTargetOut(target=paper.total_marks, actual=total),
        sections=section_out,
        bloom_mix=bloom_out,
        chapters=chapters_out,
        difficulty=DifficultyMix(easy=actual_difficulty[PaperDifficultyLevel.EASY],
                                 medium=actual_difficulty[PaperDifficultyLevel.MEDIUM],
                                 hard=actual_difficulty[PaperDifficultyLevel.HARD]),
        estimated_minutes=estimated,
        duration_minutes=paper.duration_minutes,
        repeats=repeats,
        issues=issues,
    )
