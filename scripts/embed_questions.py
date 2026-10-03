"""Embed dataset_questions into question_embeddings (pgvector) for retrieval.

Only rows whose embedding text or model changed are (re-)embedded, and
identical texts - the same question sitting in several Datasets - are sent
to the API once. OPENAI_API_KEY comes from the env or the repo's .env.

Usage (host):
    python scripts/embed_questions.py                 # embed new/changed rows
    python scripts/embed_questions.py --search "resistance when length is doubled" --grade 10
"""
import argparse
import hashlib
import os
import re
from pathlib import Path

import psycopg2
from llama_index.embeddings.openai import OpenAIEmbedding

from import_dataset import db_config, read_env

MODEL = "text-embedding-3-small"  # 1536-dim - must match question_embeddings.embedding
FIGURE = re.compile(r"\[figure:\d+\]")


def embedding_text(chapter, subtopic, fmt, marks, bloom, text, options, answer):
    """What gets embedded: the question in its curriculum context. Figures
    are dropped (no image content in the vector); the answer is capped so
    long solutions don't drown the question itself."""
    parts = [f"Chapter: {chapter}" + (f" | Subtopic: {subtopic}" if subtopic else ""),
             f"Type: {fmt or 'question'}" + (f", {marks:g} marks" if marks else "") + f", Bloom: {bloom.lower()}",
             "Question: " + FIGURE.sub("", text).strip()]
    if options:
        parts.append("Options: " + "; ".join(FIGURE.sub("", o).strip() for o in options))
    if answer:
        parts.append("Answer: " + FIGURE.sub("", answer).strip()[:500])
    return "\n".join(parts)


def vector_literal(values):
    return "[" + ",".join(f"{v:.7f}" for v in values) + "]"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--search", help="print the top matches for this query instead of embedding")
    ap.add_argument("--grade", type=int)
    ap.add_argument("--top", type=int, default=5)
    args = ap.parse_args()

    env_file = Path(__file__).resolve().parent.parent / ".env"
    api_key = os.environ.get("OPENAI_API_KEY") or read_env(env_file).get("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit("OPENAI_API_KEY is not set (env or .env).")
    embed = OpenAIEmbedding(model=MODEL, api_key=api_key, embed_batch_size=100)

    conn = psycopg2.connect(**db_config(env_file))
    with conn, conn.cursor() as cur:
        if args.search:
            cur.execute(
                "SELECT DISTINCT ON (q.external_uid) q.external_uid, 1 - (e.embedding <=> %s::vector) AS score, "
                "c.name, q.text FROM question_embeddings e "
                "JOIN dataset_questions q ON q.id = e.dataset_question_id "
                "JOIN datasets d ON d.id = q.dataset_id LEFT JOIN chapters c ON c.id = q.chapter_id "
                "WHERE %s::int IS NULL OR d.grade = %s::int "
                "ORDER BY q.external_uid, score DESC",
                (vector_literal(embed.get_query_embedding(args.search)), args.grade, args.grade))
            for uid, score, chapter, text in sorted(cur.fetchall(), key=lambda r: -r[1])[:args.top]:
                print(f"{score:.3f}  [{chapter}] {text[:110].replace(chr(10), ' ')}")
            return

        cur.execute(
            "SELECT q.id, c.name, q.subtopic_label, q.format, q.marks, q.bloom_level, q.text, q.options, q.answer, "
            "e.content_hash FROM dataset_questions q LEFT JOIN chapters c ON c.id = q.chapter_id "
            "LEFT JOIN question_embeddings e ON e.dataset_question_id = q.id")
        todo = {}  # content_hash -> (text, [row ids])
        for qid, chapter, subtopic, fmt, marks, bloom, text, options, answer, have_hash in cur.fetchall():
            content = embedding_text(chapter, subtopic, fmt, marks, bloom, text, options, answer)
            content_hash = hashlib.sha256(f"{MODEL}\n{content}".encode()).hexdigest()
            if content_hash != have_hash:
                todo.setdefault(content_hash, (content, []))[1].append(qid)
        print(f"{sum(len(ids) for _, ids in todo.values())} rows need embedding ({len(todo)} unique texts)")

        # Reuse vectors already stored under the same hash before paying for new ones.
        hashes = list(todo)
        cur.execute("SELECT DISTINCT ON (content_hash) content_hash, embedding::text FROM question_embeddings "
                    "WHERE content_hash = ANY(%s)", (hashes,))
        known = dict(cur.fetchall())
        missing = [h for h in hashes if h not in known]
        for i in range(0, len(missing), 500):
            chunk = missing[i:i + 500]
            for h, values in zip(chunk, embed.get_text_embedding_batch([todo[h][0] for h in chunk])):
                known[h] = vector_literal(values)
            print(f"  embedded {min(i + 500, len(missing))}/{len(missing)}")

        for h, (_, ids) in todo.items():
            for qid in ids:
                cur.execute(
                    "INSERT INTO question_embeddings (dataset_question_id, model, content_hash, embedding) "
                    "VALUES (%s, %s, %s, %s::vector) ON CONFLICT (dataset_question_id) DO UPDATE SET "
                    "model = EXCLUDED.model, content_hash = EXCLUDED.content_hash, embedding = EXCLUDED.embedding, "
                    "created_at = now()", (qid, MODEL, h, known[h]))
        cur.execute("SELECT count(*) FROM question_embeddings")
        print(f"question_embeddings now holds {cur.fetchone()[0]} rows")
    conn.close()


if __name__ == "__main__":
    main()
