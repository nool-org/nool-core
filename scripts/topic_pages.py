"""Finds the NCERT page each topic starts on (topics.page) from the chapter
PDFs in nool-kg, and prints the SQL that stores it. Runs on the host (it
needs the PDFs and PyMuPDF):

    docker exec nool-core-postgres-1 sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -AtF"|" -c \
      "select s.name, c.order_index, t.name, t.id from topics t join chapters c on c.id=t.chapter_id
       join subjects s on s.id=c.subject_id"' > topics.txt
    python scripts/topic_pages.py topics.txt > pages.sql   # then psql < pages.sql

A page's printed number sits in its header ("Science196", "Magnetic Effects of
Electric Current 197"), so a chapter's first number gives the rest. A topic
"12.2 MAGNETIC FIELD ..." starts on the first page whose text has "12.2 " at
a line start; "Introduction" is the chapter's first page.
"""
import re
import sys
from pathlib import Path

import fitz  # PyMuPDF - pypdf gives up on some of these PDFs

BOOKS = Path(__file__).resolve().parents[2] / "nool-kg"
PDF = {"Science": BOOKS / "Science Books" / "jesc1dd" / "jesc1{:02d}.pdf",
       "Mathematics": BOOKS / "Maths Books" / "jemh1dd" / "jemh1{:02d}.pdf"}


def first_page_number(texts: list[str]) -> int | None:
    """The printed number of the PDF's first page: each later page's header
    ("Heredity" / "129") implies one; the most common answer wins."""
    votes: dict[int, int] = {}
    for i, text in enumerate(texts[1:], start=1):
        head = " ".join(text.strip().splitlines()[:2])
        found = re.search(r"(?<![\d.])(\d{1,3})(?![\d.])", head)
        if found:
            start = int(found.group(1)) - i
            votes[start] = votes.get(start, 0) + 1
    return max(votes, key=votes.get) if votes else None


def main(topics_file: str) -> None:
    pages: dict[tuple[str, int], tuple[list[str], int | None]] = {}
    for line in Path(topics_file).read_text(encoding="utf-8").splitlines():
        subject, order, name, topic_id = line.split("|")
        key = (subject, int(order))
        if key not in pages:
            path = Path(str(PDF.get(subject, "")).format(int(order))) if subject in PDF else None
            if path is None or not path.exists():
                pages[key] = ([], None)
            else:
                texts = [page.get_text() for page in fitz.open(path)]
                pages[key] = (texts, first_page_number(texts))
        texts, start = pages[key]
        if start is None:
            print(f"-- no pages for {subject} {order}: {name}", file=sys.stderr)
            continue
        number = re.match(r"(\d+\.\d+)\s", name)
        if name.strip().lower() == "introduction":
            at = 0
        elif number:
            pattern = re.compile(rf"(^|\n)\s*{re.escape(number.group(1))}\s+\S")
            at = next((i for i, t in enumerate(texts) if pattern.search(t)), None)
        else:  # e.g. "Additional Activities": its own words on a page
            words = name.strip().upper()
            at = next((i for i, t in enumerate(texts) if words in t.upper()), None)
        if at is None:
            print(f"-- not found: {subject} {order}: {name}", file=sys.stderr)
            continue
        print(f"update topics set page = {start + at} where id = '{topic_id}';")


main(sys.argv[1])
