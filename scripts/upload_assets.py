"""Upload the images dataset_questions actually reference (figures[].blob)
to the question-assets blob container. Only referenced files are uploaded;
blobs already in the container are skipped, so this is safe to re-run.

Target storage comes from AZURE_STORAGE_CONNECTION_STRING (env or .env);
unset means the local Azurite from compose.override.yml.

Usage (host):
    python scripts/upload_assets.py --data C:/Users/manishdev/mycbseguide/data
"""
import argparse
import mimetypes
import os
from pathlib import Path

import psycopg2
from azure.core.exceptions import ResourceExistsError
from azure.storage.blob import BlobServiceClient, ContentSettings

from import_dataset import db_config, read_env

CONTAINER = "question-assets"
# Azurite's fixed, publicly documented development account - not a secret.
AZURITE = ("DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;"
           "AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==;"
           "BlobEndpoint=http://127.0.0.1:10000/devstoreaccount1;")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True, type=Path, help="scraper data dir (holds figures/)")
    ap.add_argument("--prefix", default="cbse/10/science/", help="blob prefix the import used")
    args = ap.parse_args()

    env_file = Path(__file__).resolve().parent.parent / ".env"
    conn_str = os.environ.get("AZURE_STORAGE_CONNECTION_STRING") or read_env(env_file).get(
        "AZURE_STORAGE_CONNECTION_STRING") or AZURITE

    conn = psycopg2.connect(**db_config(env_file))
    with conn, conn.cursor() as cur:
        cur.execute("SELECT DISTINCT f->>'blob' FROM dataset_questions, jsonb_array_elements(figures) f "
                    "WHERE f->>'blob' LIKE %s", (args.prefix + "%",))
        blobs = sorted(row[0] for row in cur.fetchall())
    conn.close()

    service = BlobServiceClient.from_connection_string(conn_str)
    container = service.get_container_client(CONTAINER)
    try:
        # Public read for blobs (not listing) - question images aren't
        # sensitive; switch to SAS URLs later without touching the DB.
        container.create_container(public_access="blob")
    except ResourceExistsError:
        pass
    existing = {b.name for b in container.list_blobs(name_starts_with=args.prefix)}

    uploaded, skipped, problems = 0, 0, []
    for blob in blobs:
        if blob in existing:
            skipped += 1
            continue
        path = args.data / "figures" / blob[len(args.prefix):]
        if not path.is_file() or path.stat().st_size == 0:
            problems.append(blob)
            continue
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        with open(path, "rb") as f:
            container.upload_blob(blob, f, content_settings=ContentSettings(content_type=content_type))
        uploaded += 1

    print(f"{len(blobs)} referenced images: {uploaded} uploaded, {skipped} already there, {len(problems)} missing/empty")
    for blob in problems:
        print("  missing/empty:", blob)
    print(f"container URL: {container.url}")


if __name__ == "__main__":
    main()
