"""Fill dataset_questions.difficulty / marks where the source bank had none,
using an LLM. Never overwrites a value that's already set, and records
what filled it in source->'enriched' (e.g. {"difficulty": "llm:gpt-5-mini"})
so a guessed value is never mistaken for a printed one.

Works per external_uid (the same question in several Datasets is graded
once and written to every copy). Commits per batch, so it can be stopped
and re-run - it only picks up questions still missing a value.

Usage (host):
    python scripts/enrich_questions.py --subject-name Science --grade 10 --limit 50   # trial
    python scripts/enrich_questions.py --subject-name Science --grade 10
"""
import argparse
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psycopg2
from openai import OpenAI

from import_dataset import db_config, read_env

MODEL = "gpt-5-mini"
BATCH = 25
# Marks a question of each format can carry in a CBSE Class 10 paper - the
# LLM only ever picks from these, and only for formats that need it.
ALLOWED_MARKS = {"short": [2, 3], "numerical": [1, 2, 3, 5]}

SYSTEM = """You grade CBSE Class 10 Science questions for an exam-paper generator.
For each question return its difficulty for an average Class 10 student:
- EASY: direct recall or a single-step fact/definition/identification.
- MEDIUM: explanation, reasoning over 2-3 ideas, or a routine one/two-step application or numerical.
- HARD: multi-step numericals, analysing unfamiliar data/diagrams/situations, or evaluating/justifying across concepts.
When "marks_options" is given, also choose the marks from that list that CBSE would most likely award,
judging by how much the expected answer has to cover. Otherwise return marks as null.
Return one result per input id, in any order."""

SCHEMA = {
    "name": "grades",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["results"],
        "properties": {"results": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["id", "difficulty", "marks"],
            "properties": {
                "id": {"type": "string"},
                "difficulty": {"type": "string", "enum": ["EASY", "MEDIUM", "HARD"]},
                "marks": {"type": ["integer", "null"]},
            },
        }}},
    },
}


def grade(client, batch):
    items = [{
        "id": q["uid"], "chapter": q["chapter"], "type": q["format"], "bloom": q["bloom"],
        "question": q["text"][:1500], "expected_answer": (q["answer"] or "")[:800],
        **({"marks_options": ALLOWED_MARKS[q["format"]]} if q["need_marks"] else {}),
    } for q in batch]
    response = client.chat.completions.create(
        model=MODEL, reasoning_effort="minimal",
        response_format={"type": "json_schema", "json_schema": SCHEMA},
        messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": json.dumps(items, ensure_ascii=False)}],
    )
    by_id = {r["id"]: r for r in json.loads(response.choices[0].message.content)["results"]}
    out = []
    for q in batch:  # validate - anything off is simply left for the next run
        r = by_id.get(q["uid"])
        if not r:
            continue
        marks = r["marks"] if q["need_marks"] and r["marks"] in ALLOWED_MARKS[q["format"]] else None
        out.append((q["uid"], r["difficulty"] if q["need_difficulty"] else None, marks))
    return out, response.usage

# the subject's catalog datasets - never a school's own question bank
CATALOG = "SELECT id FROM datasets WHERE subject_id = %s AND school_id IS NULL"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, help="grade at most this many questions (trial run)")
    ap.add_argument("--workers", type=int, default=8)
    # external_uids repeat across subjects/grades (every graph starts at CH01), so a run is one subject's catalog
    ap.add_argument("--subject-name", required=True, help='subjects.name, e.g. "Science"')
    ap.add_argument("--grade", type=int, required=True)
    args = ap.parse_args()

    env_file = Path(__file__).resolve().parent.parent / ".env"
    client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY") or read_env(env_file).get("OPENAI_API_KEY"))
    conn = psycopg2.connect(**db_config(env_file))
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM subjects WHERE name = %s AND grade = %s", (args.subject_name, args.grade))
        found = cur.fetchone()
        if found is None:
            raise SystemExit(f'No subject "{args.subject_name}" for grade {args.grade}.')
        subject_id = found[0]
        cur.execute(
            "SELECT DISTINCT ON (q.external_uid) q.external_uid, c.name, q.format, q.bloom_level, q.text, q.answer, "
            "bool_or(q.difficulty IS NULL) OVER w, bool_or(q.marks IS NULL) OVER w "
            "FROM dataset_questions q LEFT JOIN chapters c ON c.id = q.chapter_id "
            "WHERE q.external_uid IS NOT NULL AND q.dataset_id IN (" + CATALOG + ") "
            "WINDOW w AS (PARTITION BY q.external_uid) ORDER BY q.external_uid", (subject_id,))
        todo = [{"uid": u, "chapter": ch, "format": f, "bloom": b, "text": t, "answer": a,
                 "need_difficulty": nd, "need_marks": nm and f in ALLOWED_MARKS}
                for u, ch, f, b, t, a, nd, nm in cur.fetchall()]
    todo = [q for q in todo if q["need_difficulty"] or q["need_marks"]][:args.limit]
    batches = [todo[i:i + BATCH] for i in range(0, len(todo), BATCH)]
    print(f"{len(todo)} questions to grade "
          f"({sum(q['need_difficulty'] for q in todo)} difficulty, {sum(q['need_marks'] for q in todo)} marks) "
          f"in {len(batches)} calls")

    done = tokens_in = tokens_out = failed = 0
    with ThreadPoolExecutor(args.workers) as pool:
        for future in [pool.submit(grade, client, b) for b in batches]:
            try:
                results, usage = future.result()
            except Exception as e:  # one bad call shouldn't lose the rest - re-run picks it up
                failed += 1
                print("  batch failed:", type(e).__name__, str(e)[:120])
                continue
            tokens_in, tokens_out = tokens_in + usage.prompt_tokens, tokens_out + usage.completion_tokens
            with conn, conn.cursor() as cur:
                for uid, difficulty, marks in results:
                    enriched = {k: f"llm:{MODEL}" for k, v in (("difficulty", difficulty), ("marks", marks)) if v}
                    cur.execute(
                        "UPDATE dataset_questions SET "
                        "difficulty = COALESCE(difficulty, %s::paper_difficulty_level), "
                        "marks = COALESCE(marks, %s), "
                        "source = jsonb_set(COALESCE(source, '{}'), '{enriched}', "
                        "COALESCE(source->'enriched', '{}') || %s::jsonb) "
                        "WHERE external_uid = %s AND dataset_id IN (" + CATALOG + ")",
                        (difficulty, marks, json.dumps(enriched), uid, subject_id))
            done += len(results)
            if done and done % 1000 < BATCH:
                print(f"  graded {done}/{len(todo)}")
    conn.close()
    print(f"graded {done}/{len(todo)}, {failed} failed batches; tokens in={tokens_in:,} out={tokens_out:,}")


if __name__ == "__main__":
    main()
