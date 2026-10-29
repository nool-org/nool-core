"""Can this be answered aloud? The one check every spoken question goes
through: voice-test prep (voice_test_prep) and daily practice's spoken
answers (daily_practice import, practice_voice serving).

problem(): a bank question as written - one that needs options, a page or
written work never becomes a spoken question.
spoken_problem(): a wording already made for speech - ONE short ask a
student can hold in mind when heard once, with hints that only ask.
Each returns why not (a short reason, logged), or None when it is fine.
"""

import re

from src.domain.models import QuestionType

# A stem with its options written in: "... (a) 2 (b) 4 ..." / "... A. 2 B. 4 ..." / "State True or False".
_LISTED_OPTIONS = re.compile(r"(?:^|\s)\(?[aA][.)]\s+\S.*(?:^|\s)\(?[bB][.)]\s+\S|\btrue or false\b", re.S | re.I)
_LATEX = re.compile(r"\$[^$\n]+\$|\\[a-zA-Z]{2,}")  # $2NH_3$, \rightarrow
_BLANKS = re.compile(r"_{3,}|\bfill in\b", re.I)
_DRAWING = re.compile(r"\b(draw|diagram|sketch|label|figure|graph|plot)\b", re.I)
_PASSAGE = re.compile(r"read the (following|passage|text)|questions? that follows?|\btable\b|case[- ]based", re.I)
_PARTS = re.compile(r"(?:^|\s)\(?(?:i{1,3}|iv|[a-d])[.)]\s")  # (i) ... (ii) ... / a. ... b. ...
_LISTED_LINES = re.compile(r":[ \t]*\n[ \t]*\S[^\n]*\n[ \t]*\S")  # "Give reasons for the following:\nA\nB"
_OR_JOINED = re.compile(r"\s(?:OR|Or)\s")  # "State X. OR How is Y ..."
# Written work rewording can't turn into one spoken answer. ("write any four ..." is NOT here:
# generation narrows it to "tell me two or three ...".)
_WRITTEN_WORK = re.compile(r"\btabulate\b|match the following|\bcomplete the (?:table|equation)"
                           r"|\bbalance the (?:following )?(?:chemical )?equations?", re.I)
# An equation or formula is written work even when the ask says "say" or "tell" (C-001).
_EQUATION = re.compile(r"\bchemical equations?\b|\b(?:write|give|say|tell|state)\b[^.?!\n]*\b(?:equations?|formulae?)\b",
                       re.I)
_CALCULATE = re.compile(r"\bcalculate\b|\bfind the value", re.I)
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
MAX_WORDS = 45  # a bank question longer than this is too much to hold in mind when heard once

MAX_SPOKEN_WORDS = 30  # generation's MAX_QUESTION_WORDS - a spoken wording is about 20
_SENTENCES = re.compile(r"(?<=[.?!])\s+|\n+")
_ASK = r"(?:tell|give|say|name|define|explain|state|describe|list|identify|mention)"
_ASK_FIRST = re.compile(rf"^\W*(?:and\s+|also,?\s+|then,?\s+)?{_ASK}\b", re.I)
_ASK_JOINED = re.compile(rf"(?:\band|,)\s+(?:also\s+|then\s+)?{_ASK}\b", re.I)  # "... and say the ..."
_CHAINED = re.compile(r"(?:^|[.?!]\s+)(?:and\s+)?(?:also|then)\b|,\s*(?:and\s+)?also\b", re.I)


def problem(text: str | None, question_type: QuestionType | None = None) -> str | None:
    """Why a bank question can't be asked aloud, or None when it can."""
    text = text or ""
    if question_type in (QuestionType.MCQ, QuestionType.TRUE_FALSE) or _LISTED_OPTIONS.search(text):
        return "options"  # only makes sense with options to read
    if "{tex}" in text.lower() or _LATEX.search(text):
        return "tex"  # markup read out letter by letter
    if "�" in text:  # stray encoding characters
        return "encoding"
    if _BLANKS.search(text):
        return "blanks"
    if _DRAWING.search(text):
        return "drawing"
    if _PASSAGE.search(text):
        return "passage"
    if _WRITTEN_WORK.search(text):
        return "written_work"
    if _EQUATION.search(text):
        return "equation"
    if _CALCULATE.search(text) and len(_NUMBER.findall(text)) >= 3:
        return "long_numerical"  # a multi-step sum is worked on paper, not held in mind
    if len(_PARTS.findall(text)) >= 2 or _LISTED_LINES.search(text) or _OR_JOINED.search(text):
        return "multipart"
    if len(text.split()) > MAX_WORDS:
        return "too_long"
    return None


def spoken_problem(spoken: str | None, hints: list | None = None) -> str | None:
    """Why a wording made for speech still can't be asked aloud, or None."""
    spoken = (spoken or "").strip()
    if not spoken:
        return "empty"
    if reason := problem(spoken):
        return reason
    if spoken.count("?") > 1:
        return "several_questions"
    if len(spoken.split()) > MAX_SPOKEN_WORDS:
        return "too_long"
    sentences = [s for s in _SENTENCES.split(spoken) if s.strip()]
    asks = (sum(bool(_ASK_FIRST.search(s)) or s.rstrip().endswith("?") for s in sentences)
            + len(_ASK_JOINED.findall(spoken)))
    if len(sentences) > 2 or asks > 1 or _CHAINED.search(spoken):
        return "several_asks"  # "Define ... Give ... Also say ..." has no "?" but asks three things
    if not all(isinstance(h, str) and h.strip().endswith("?") for h in hints or []):
        return "hint_tells"  # a hint only ever asks
    return None
