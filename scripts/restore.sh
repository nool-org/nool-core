#!/usr/bin/env bash
# Restores a dump written by the production "backup" service (see
# scripts/backup.sh / compose.prod.yml) into the stack's postgres.
#
# Usage (from the repo root on the server):
#   ./scripts/restore.sh                       # list available dumps
#   ./scripts/restore.sh nool-20261005T210000Z.dump
#
# Notes:
# - DESTRUCTIVE: --clean drops and recreates every object in the dump, so the
#   database ends up exactly as it was at dump time; anything written since
#   is lost. Take a fresh `exec backup sh /backup.sh once` first if unsure.
# - Stop the writers first so nothing races the restore, then start them again:
#     docker compose -f compose.yml -f compose.prod.yml stop core auth colearner
#     ./scripts/restore.sh <file>
#     docker compose -f compose.yml -f compose.prod.yml start core auth colearner
# - The dump includes alembic_version; if it predates the running image's
#   migrations, run them again: docker compose ... run --rm migrate
# - To pull a dump off the server:
#     docker compose -f compose.yml -f compose.prod.yml cp backup:/backups/<file> .
# - COMPOSE_PROJECT_NAME (or COMPOSE_ARGS, e.g. "-p nool-prodtest") targets
#   another project.
set -euo pipefail

cd "$(dirname "$0")/.."

compose() {
    # shellcheck disable=SC2086
    docker compose -f compose.yml -f compose.prod.yml ${COMPOSE_ARGS:-} "$@"
}

if [ "$#" -eq 0 ]; then
    compose exec -T backup sh -c 'ls -lh /backups'
    exit 0
fi

file=$(basename "$1")
compose exec -T backup sh -c \
    'pg_restore --clean --if-exists --no-owner --exit-on-error --single-transaction -h "${PGHOST:-postgres}" -d "$PGDATABASE" "/backups/$0"' \
    "$file"
echo "restored $file"
