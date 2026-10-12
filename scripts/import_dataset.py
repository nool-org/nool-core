"""Bulk-import a subject's merged question bank into datasets/dataset_questions.

Reads the scraper's merged bank (<data>/questionbank/Class_<grade>/<Subject>/
*.json - one file per chapter) and writes one QA Dataset per source category
(see DATASETS), one DatasetQuestion row per (dataset, question). Idempotent:
re-running upserts on (dataset_id, external_uid), except rows a Super Admin
has edited (source.edited), which are left as they are.

Images are NOT uploaded here - rows only carry blob paths (figures[].blob);
push the files with a separate upload step once the storage account exists.

Usage (host, against the compose Postgres on localhost:5432; creds from .env):
    python scripts/import_dataset.py --data C:/Users/manishdev/mycbseguide/data
    python scripts/import_dataset.py --data ... --dry-run   # convert + check only
"""
import argparse
import json
import re
import uuid
from pathlib import Path

import psycopg2
from psycopg2.extras import Json

# Source category tags (sources[].dataset) -> Dataset name.
DATASETS = {
    "Previous Year Questions": ["previous-year-questions-bank", "pdf-previous-year-papers-online", "pdf-previous-year-papers"],
    "Important Questions": ["pdf-important-questions", "important-questions-bank"],
    "NCERT Solutions": ["ncert-solutions-qa", "pdf-ncert-solutions"],
    "NCERT Exemplar": ["ncert-exemplar-solutions-qa", "pdf-ncert-exemplar-solutions"],
    "Sample Papers": ["pdf-sample-papers"],
    "Practice MCQs": ["practice-questions"],
    "Test Papers": ["pdf-test-papers", "online-tests"],
    "Case-based & Value-based Questions": ["pdf-case-study-questions", "pdf-value-based-questions"],
}
TAG_TO_DATASET = {tag: name for name, tags in DATASETS.items() for tag in tags}

# NCERT Class 10 Science chapter order - kg_ref CH01..CH13 matches nool-kg's
# Chapter ids, so a later POST /admin/curriculum/sync-from-kg upserts these
# rows (renaming them to the KG's titles) instead of duplicating them.
CHAPTERS = [
    "Chemical_Reactions_and_Equations", "Acids_Bases_and_Salts", "Metals_and_Non-metals",
    "Carbon_and_its_Compounds", "Life_Processes", "Control_and_Coordination",
    "How_do_Organisms_Reproduce", "Heredity_and_Evolution", "Light_Reflection_and_Refraction",
    "Human_Eye_and_Colourful_World", "Electricity", "Magnetic_Effects_of_Electric_Current",
    "Our_Environment",
]

FORMAT_TO_TYPE = {
    "mcq": "MCQ", "assertion-reason": "MCQ", "very-short": "SHORT_ANSWER", "short": "SHORT_ANSWER",
    "numerical": "SHORT_ANSWER", "long": "LONG_ANSWER", "case-based": "LONG_ANSWER",
}
BLOOM = {"remember": "REMEMBER", "understand": "UNDERSTAND", "apply": "APPLY",
         "analyse": "ANALYZE", "analyze": "ANALYZE", "evaluate": "EVALUATE", "create": "CREATE"}
DIFFICULTY = {"easy": "EASY", "medium": "MEDIUM", "hard": "HARD"}
MARKER = re.compile(r"\[image: ([^\]]+)\]")


def convert(q, blob_prefix, site_index, data):
    """One source question -> DatasetQuestion column values (minus ids/FKs)."""
    figures, by_url = [], {}

    def add_figure(file, **meta):
        if not (data / file).is_file() or not (data / file).stat().st_size:
            return None  # broken download (empty/missing) - never reference it
        figures.append({"blob": blob_prefix + file.removeprefix("figures/"), **meta})
        return len(figures) - 1

    for g in q.get("figures") or []:
        if g.get("file"):
            i = add_figure(g["file"], role=g.get("role"), option=g.get("option"),
                           caption=g.get("caption"), kind=g.get("kind"))
            if g.get("url") and i is not None:
                by_url[g["url"]] = i

    def link(text):
        # [image: <url>] -> [figure:N]; a marker with no local image is dropped.
        def sub(m):
            url = m.group(1)
            if url not in by_url and isinstance(site_index.get(url), str):
                by_url[url] = add_figure(site_index[url], role=None, option=None, caption=None, kind=None)
            return f"[figure:{by_url[url]}]" if by_url.get(url) is not None else ""
        return MARKER.sub(sub, (text or "").replace("\x00", "")).strip()  # Postgres rejects NUL

    text, solution = link(q["question"]), link(q.get("solution"))
    options = [link(o) for o in q.get("options") or []]
    letter = (q.get("answer") or "").strip().upper()
    fmt = q["type"]
    qtype = FORMAT_TO_TYPE[fmt]

    if options and [o.lower() for o in options] == ["true", "false"] and letter in ("A", "B"):
        qtype, answer = "TRUE_FALSE", options["AB".index(letter)]
    elif options and len(letter) == 1 and "A" <= letter < chr(ord("A") + len(options)):
        qtype, answer = "MCQ", options[ord(letter) - ord("A")]
    else:
        # No usable option key (matching columns, missing answer letter): the
        # options become part of the question and the solution is the answer.
        if options:
            text += "\n" + "\n".join(f"({chr(97 + i)}) {o}" for i, o in enumerate(options))
        qtype = "LONG_ANSWER" if qtype == "LONG_ANSWER" else "SHORT_ANSWER"
        options, answer = None, solution or letter

    return {
        "external_uid": q["uid"], "format": fmt, "question_type": qtype,
        "bloom_level": BLOOM[q["bloom_level"]], "text": text, "options": options or None,
        "answer": answer, "solution": solution or None, "marks": q.get("marks"),
        "difficulty": DIFFICULTY.get((q.get("difficulty") or "").lower()),
        "concepts": q.get("concepts") or None, "subtopic_label": q.get("subtopic"),
        "years_asked": [int(y) for y in q.get("years_asked") or [] if str(y).isdigit()] or None,
        "figures": figures or None,
        "source": {"sources": q["sources"], "answer_confidence": q.get("answer_confidence")},
    }


def check(row):
    """Same invariants as the admin API's _check_dataset_question_options."""
    if row["question_type"] in ("MCQ", "TRUE_FALSE"):
        assert row["options"] and len(row["options"]) >= 2 and row["answer"] in row["options"], row["external_uid"]
    else:
        assert row["options"] is None, row["external_uid"]
    assert row["text"], row["external_uid"]


def read_env(env_file):
    return dict(line.split("=", 1) for line in env_file.read_text().splitlines()
                if "=" in line and not line.lstrip().startswith("#"))


def db_config(env_file):
    env = read_env(env_file)
    return {"host": "127.0.0.1", "port": int(env.get("POSTGRES_PORT_HOST", 5432)), "user": env["POSTGRES_USER"],
            "password": env["POSTGRES_PASSWORD"], "dbname": env["POSTGRES_DB"]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True, type=Path)
    ap.add_argument("--board", default="CBSE")
    ap.add_argument("--grade", type=int, default=10)
    ap.add_argument("--subject-dir", default="Science")
    ap.add_argument("--subject-name", default="Science", help="existing subjects.name (for --grade)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    blob_prefix = f"{args.board.lower()}/{args.grade}/{args.subject_dir.lower()}/"
    site_index = json.loads((args.data / "figures/site/_index.json").read_text(encoding="utf-8"))
    bank = args.data / f"questionbank/Class_{args.grade}/{args.subject_dir}"

    rows = []  # (chapter file stem, dataset name, row)
    for n, stem in enumerate(CHAPTERS, 1):
        seen = {}
        for q in json.loads((bank / f"{stem}.json").read_text(encoding="utf-8"))["questions"]:
            row = convert(q, blob_prefix, site_index, args.data)
            check(row)
            # Source uids are only unique per chapter (a few not even that) -
            # scope by chapter, then disambiguate repeats by file order.
            seen[q["uid"]] = seen.get(q["uid"], 0) + 1
            row["external_uid"] = f"CH{n:02d}:{q['uid']}" + (f"#{seen[q['uid']]}" if seen[q["uid"]] > 1 else "")
            for name in sorted({TAG_TO_DATASET[s["dataset"]] for s in q["sources"]}):
                rows.append((stem, name, row))
    print(f"converted {len(rows)} dataset rows from {len({r['external_uid'] for _, _, r in rows})} questions")
    if args.dry_run:
        return

    conn = psycopg2.connect(**db_config(Path(__file__).resolve().parent.parent / ".env"))
    with conn, conn.cursor() as cur:
        # subjects are per grade: "Science" exists once for each grade
        cur.execute("SELECT id FROM subjects WHERE name = %s AND grade = %s", (args.subject_name, args.grade))
        found = cur.fetchone()
        if found is None:
            raise SystemExit(f'No subject "{args.subject_name}" for grade {args.grade} - create it first.')
        subject_id = found[0]
        cur.execute("SELECT id FROM users WHERE role = 'SUPER_ADMIN' ORDER BY email LIMIT 1")
        created_by = cur.fetchone()[0]

        chapter_ids = {}
        for n, stem in enumerate(CHAPTERS, 1):
            ref = f"CH{n:02d}"
            cur.execute("SELECT id FROM chapters WHERE subject_id = %s AND kg_ref = %s", (subject_id, ref))
            found = cur.fetchone()
            if not found:
                cur.execute("INSERT INTO chapters (id, subject_id, name, order_index, grade, kg_ref) "
                            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                            (str(uuid.uuid4()), subject_id, stem.replace("_", " "), n, args.grade, ref))
                found = cur.fetchone()
            chapter_ids[stem] = found[0]

        dataset_ids = {}
        for name, tags in DATASETS.items():
            cur.execute("SELECT id FROM datasets WHERE name = %s AND subject_id = %s AND grade = %s AND board = %s",
                        (name, subject_id, args.grade, args.board))
            found = cur.fetchone()
            if not found:
                cur.execute("INSERT INTO datasets (id, name, question_count, description, restricted, type, "
                            "subject_id, board, grade) VALUES (%s, %s, 0, %s, false, 'QA', %s, %s, %s) RETURNING id",
                            (str(uuid.uuid4()), name, f"{args.board} Class {args.grade} {args.subject_dir} - {name}",
                             subject_id, args.board, args.grade))
                found = cur.fetchone()
            dataset_ids[name] = found[0]

        cols = ["external_uid", "format", "question_type", "bloom_level", "text", "options", "answer", "solution",
                "marks", "difficulty", "concepts", "subtopic_label", "years_asked", "figures", "source"]
        casts = {"question_type": "::question_type", "bloom_level": "::bloom_level",
                 "difficulty": "::paper_difficulty_level"}
        # marks/difficulty keep an existing value when the source has none -
        # scripts/enrich_questions.py fills those, and a re-import mustn't
        # wipe them; source keeps its `enriched` provenance for the same reason.
        keep = {"marks": "COALESCE(EXCLUDED.marks, dataset_questions.marks)",
                "difficulty": "COALESCE(EXCLUDED.difficulty, dataset_questions.difficulty)",
                "source": "EXCLUDED.source || jsonb_strip_nulls(jsonb_build_object("
                          "'enriched', dataset_questions.source->'enriched'))"}
        sql = (f"INSERT INTO dataset_questions (id, dataset_id, chapter_id, created_by, {', '.join(cols)}) "
               f"VALUES (%s, %s, %s, %s, {', '.join('%s' + casts.get(c, '') for c in cols)}) "
               f"ON CONFLICT (dataset_id, external_uid) DO UPDATE SET chapter_id = EXCLUDED.chapter_id, "
               + ", ".join(f"{c} = {keep.get(c, 'EXCLUDED.' + c)}" for c in cols[1:])
               # a Super Admin's edit (question_bank.propagate_catalog_edit) wins over the source
               + " WHERE dataset_questions.source->>'edited' IS NULL")
        for stem, name, row in rows:
            values = [Json(row[c]) if c in ("options", "figures", "source") and row[c] is not None else row[c]
                      for c in cols]
            cur.execute(sql, [str(uuid.uuid4()), dataset_ids[name], chapter_ids[stem], created_by, *values])

        for name, dataset_id in dataset_ids.items():
            cur.execute("UPDATE datasets SET question_count = (SELECT count(*) FROM dataset_questions "
                        "WHERE dataset_id = %s) WHERE id = %s RETURNING question_count", (dataset_id, dataset_id))
            print(f"  {name}: {cur.fetchone()[0]}")
    conn.close()


if __name__ == "__main__":
    main()
