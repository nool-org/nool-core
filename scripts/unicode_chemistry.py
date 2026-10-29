"""C-007 (b): simple chemistry written in LaTeX - arrows, element subscripts and ion
charges such as "{tex}2NaOH + H_2SO_4 \\to Na_2SO_4{/tex}" or "$\\mathrm{H}_{2}\\mathrm{O}$" -
becomes plain Unicode (2NaOH + H₂SO₄ → Na₂SO₄, H₂O) in every table students and
teachers read. A span with anything else (fractions, Maths, unknown commands) is left
for the app's maths renderer. A bare \\rightarrow outside a span is converted too.
Converted text has no markup left, so re-running changes nothing. Without --go it
only counts. Runs inside the core image:

    docker compose run --rm -v "$PWD/scripts:/s:ro" core python /s/unicode_chemistry.py [--go]
"""
import asyncio
import json
import re
import sys

sys.path.insert(0, "/app")

ARROWS = {"longrightarrow": "→", "rightarrow": "→", "to": "→", "rightleftharpoons": "⇌",
          "leftarrow": "←", "uparrow": "↑", "downarrow": "↓"}
SUB = str.maketrans("0123456789+-", "₀₁₂₃₄₅₆₇₈₉₊₋")
SUP = str.maketrans("0123456789+-", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻")
SPAN = re.compile(r"\{tex\}(.*?)\{/tex\}|\$([^$\n]+?)\$", re.S)
ARROW = re.compile(r"\\(" + "|".join(sorted(ARROWS, key=len, reverse=True)) + r")(?![A-Za-z])")
CHEMISTRY = re.compile(r"\\mathrm|\\text|" + ARROW.pattern + r"|[A-Z][a-z]?\)?_\{?\d")


def chem(tex: str) -> str | None:
    """The span as Unicode text, or None when it isn't simple chemistry."""
    if not CHEMISTRY.search(tex):
        return None
    s = re.sub(r"\\(?:mathrm|text)\s*\{([^{}]*)\}", r"\1", tex)
    s = ARROW.sub(lambda m: f" {ARROWS[m.group(1)]} ", s)
    s = re.sub(r"_\{([0-9]+)\}|_([0-9])", lambda m: (m.group(1) or m.group(2)).translate(SUB), s)
    s = re.sub(r"\^\{([0-9]*[+-])\}|\^([+-])", lambda m: (m.group(1) or m.group(2)).translate(SUP), s)
    s = re.sub(r"\\[,;: ]|~", " ", s)
    if re.search(r"[\\_^{}$]", s):
        return None  # something we don't convert - leave it to the renderer
    return re.sub(r"\s{2,}", " ", s).strip()


def convert(text: str | None) -> str | None:
    """`text` with its simple chemistry spans (and bare arrows) in Unicode."""
    if not text:
        return text
    out, at = [], 0
    for m in SPAN.finditer(text):  # bare arrows only outside spans; a kept span stays whole TeX
        out += [ARROW.sub(lambda a: ARROWS[a.group(1)], text[at:m.start()]),
                chem(m.group(1) if m.group(1) is not None else m.group(2)) or m.group(0)]
        at = m.end()
    return "".join(out) + ARROW.sub(lambda a: ARROWS[a.group(1)], text[at:])


COLUMNS = {
    "dataset_questions": ("text", "answer", "solution", "options"),
    "mcq_test_questions": ("text", "explanation", "hint", "options"),
    "homework_questions": ("text", "answer"),
    "question_paper_questions": ("text", "answer", "options"),
    "practice_questions": ("text", "explanation", "hint", "options"),
}


async def main(args: list[str]) -> None:
    from sqlalchemy import text as sql

    from src.config.settings import get_settings
    from src.repositories import get_session
    from src.repositories.database import init_engine

    go = "--go" in args
    init_engine(get_settings().database_url)
    async with get_session() as session:
        for table, columns in COLUMNS.items():
            rows = (await session.execute(sql(
                f"SELECT id, {', '.join(columns)} FROM {table} WHERE "
                + " OR ".join(f"strpos({c}::text, chr(92)) > 0 OR strpos({c}::text, '$') > 0"
                              for c in columns)))).all()
            changed = 0
            for row in rows:
                updates = {}
                for column, value in zip(columns, row[1:]):
                    if column == "options" and isinstance(value, list):
                        new = [convert(o) if isinstance(o, str) else o for o in value]
                        if new != value:
                            updates[column] = json.dumps(new, ensure_ascii=False)
                    elif isinstance(value, str) and (new := convert(value)) != value:
                        updates[column] = new
                if updates:
                    changed += 1
                    sets = ", ".join(f"{c} = CAST(:{c} AS jsonb)" if c == "options" else f"{c} = :{c}"
                                     for c in updates)
                    await session.execute(sql(f"UPDATE {table} SET {sets} WHERE id = :id"), {**updates, "id": row[0]})
            print(f"{table}: {changed} of {len(rows)} candidate rows converted")
        if go:
            await session.commit()
        else:
            print("dry run - add --go to apply")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
