"""T-028: the General Instructions are written once, so their section lines go stale when the
blueprint changes. Re-derive just those lines from the current sections (letters by position);
teacher-written lines are left alone."""
import re

_SECTION_LINE = re.compile(r"^Section ([A-Z])\b(.*?)\b(consists of|has) \d+ questions? of \d+ marks? each\.?$")
_DIVIDED_LINE = re.compile(r"^This question paper is divided into \d+ sections?\b")


def sync_instructions(lines: list[str] | None, sections: list) -> list[str] | None:
    """`sections`: anything with question_count and marks_each, in paper order."""
    if not lines:
        return lines
    out: list[str] = []
    for line in lines:
        m = _SECTION_LINE.match(line.strip())
        if m:
            i = ord(m.group(1)) - ord("A")
            if i >= len(sections):
                continue  # that section no longer exists
            s = sections[i]
            q = "question" if s.question_count == 1 else "questions"
            marks = "mark" if s.marks_each == 1 else "marks"
            out.append(f"Section {m.group(1)}{m.group(2)}{m.group(3)} {s.question_count} {q} of {s.marks_each} {marks} each.")
        elif _DIVIDED_LINE.match(line.strip()):
            if len(sections) > 1:
                names = ", ".join(f"Section {chr(65 + i)}" for i in range(len(sections)))
                out.append(f"This question paper is divided into {len(sections)} sections — {names}.")
        else:
            out.append(line)
    return out
