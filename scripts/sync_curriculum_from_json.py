"""Upsert chapters/topics/subtopics from nool-kg's per-chapter JSON export
(nool-kg/output/<Class>_<Subject>/chNN_*.json) - the same rows
POST /admin/curriculum/sync-from-kg writes (see admin_catalog.py's
sync_curriculum_from_kg), matched on the same kg_ref node ids, so a later
real KG sync updates these rows instead of duplicating them. For when the
nool-kg API isn't reachable.

Usage (host):
    python scripts/sync_curriculum_from_json.py --dir C:/pet/nool-kg/output/Class10_Science --chapters 2-13 \
        --legacy-ch1 "C:/pet/nool-kg/notebooks/10_Science_1,json"

Also fills topics/subtopics.text_content (the section's textbook text -
grounding for generated questions). --legacy-ch1 only fills text for the
chapter-1 rows that already exist (their structure came from the original
KG sync and is left alone), indexed the way NB005 loaded that JSON.
"""
import argparse
import json
import uuid
from pathlib import Path

import psycopg2

from import_dataset import db_config


def chapter_list(spec: str) -> list[int]:
    out = []
    for part in spec.split(","):
        a, _, b = part.partition("-")
        out += range(int(a), int(b or a) + 1)
    return out


def upsert(cur, table, kg_ref, parent_col, parent_id, name, extra=None):
    extra = extra or {}
    cur.execute(f"SELECT id FROM {table} WHERE kg_ref = %s AND {parent_col} = %s", (kg_ref, parent_id))
    row = cur.fetchone()
    cols = {parent_col: parent_id, "name": name, **extra}
    if row:
        cur.execute(f"UPDATE {table} SET " + ", ".join(f"{c} = %s" for c in cols) + " WHERE id = %s",
                    (*cols.values(), row[0]))
        return row[0], False
    new_id = str(uuid.uuid4())
    cur.execute(f"INSERT INTO {table} (id, kg_ref, {', '.join(cols)}) VALUES (%s, %s, {', '.join(['%s'] * len(cols))})",
                (new_id, kg_ref, *cols.values()))
    return new_id, True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True, type=Path)
    ap.add_argument("--chapters", default="2-13")
    ap.add_argument("--subject-name", default="Science", help="existing subjects.name (for the export's grade)")
    ap.add_argument("--grade", type=int, default=10)
    ap.add_argument("--legacy-ch1", type=Path, help="original NB002 chapter-1 JSON: fill text for existing CH01 rows")
    args = ap.parse_args()

    conn = psycopg2.connect(**db_config(Path(__file__).resolve().parent.parent / ".env"))
    with conn, conn.cursor() as cur:
        # subjects are per grade: "Science" exists once for each grade
        cur.execute("SELECT id FROM subjects WHERE name = %s AND grade = %s", (args.subject_name, args.grade))
        found = cur.fetchone()
        if found is None:
            raise SystemExit(f'No subject "{args.subject_name}" for grade {args.grade} - create it first.')
        subject_id = found[0]
        for n in chapter_list(args.chapters):
            doc = json.loads(next(args.dir.glob(f"ch{n:02d}_*.json")).read_text(encoding="utf-8"))
            ch = doc["chapter"]
            chapter_id, _ = upsert(cur, "chapters", ch["id"], "subject_id", subject_id, ch["title"],
                                   {"order_index": ch["number"], "grade": doc["grade"]})
            created = {"topics": 0, "subtopics": 0}
            for t in ch["topics"]:
                topic_id, new = upsert(cur, "topics", t["id"], "chapter_id", chapter_id, t["title"],
                                       {"text_content": t["text_content"]})
                created["topics"] += new
                for s in t["subtopics"]:
                    _, new = upsert(cur, "subtopics", s["id"], "topic_id", topic_id, s["title"],
                                    {"text_content": s["text_content"]})
                    created["subtopics"] += new
            print(f"  {ch['id']} {ch['title']!r}: {len(ch['topics'])} topics, "
                  f"{sum(len(t['subtopics']) for t in ch['topics'])} subtopics (new: {created})")
        if args.legacy_ch1:
            filled = 0
            legacy = json.loads(args.legacy_ch1.read_text(encoding="utf-8"))["Subject"]["Grade"]["Chapter"]
            for ti, t in enumerate(legacy["Topic"], 1):
                tid = f"CH01_T{ti:02d}"
                cur.execute("UPDATE topics SET text_content = %s WHERE kg_ref = %s AND chapter_id = "
                            "(SELECT id FROM chapters WHERE subject_id = %s AND kg_ref = 'CH01')",
                            (t.get("text_content", ""), tid, subject_id))
                filled += cur.rowcount
                subs = t.get("Subtopic", [])
                for si, sub in enumerate([subs] if isinstance(subs, dict) else subs, 1):
                    cur.execute("UPDATE subtopics SET text_content = %s WHERE kg_ref = %s AND topic_id = "
                                "(SELECT t.id FROM topics t JOIN chapters c ON c.id = t.chapter_id "
                                "WHERE c.subject_id = %s AND c.kg_ref = 'CH01' AND t.kg_ref = %s)",
                                (sub.get("text_content", ""), f"{tid}_S{si:02d}", subject_id, tid))
                    filled += cur.rowcount
            print(f"  CH01 text filled on {filled} existing rows (structure untouched)")
    conn.close()


if __name__ == "__main__":
    main()
