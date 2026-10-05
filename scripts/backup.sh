#!/bin/sh
# Nightly pg_dump for compose.prod.yml's "backup" service (runs inside the
# postgres image, so pg_dump matches the server's major version).
#
#   sh /backup.sh          # loop: dump every day at BACKUP_HOUR_UTC, forever
#   sh /backup.sh once     # one dump now, e.g.:
#     docker compose -f compose.yml -f compose.prod.yml exec backup sh /backup.sh once
#
# Dumps are custom format (pg_restore-able) in /backups, named
# nool-YYYYmmddTHHMMSSZ.dump; files older than BACKUP_KEEP_DAYS are deleted
# after each successful dump. Restore with scripts/restore.sh.
set -eu

: "${PGHOST:=postgres}" "${PGUSER:?}" "${PGPASSWORD:?}" "${PGDATABASE:?}"
export PGHOST PGUSER PGPASSWORD PGDATABASE
BACKUP_DIR=${BACKUP_DIR:-/backups}
BACKUP_KEEP_DAYS=${BACKUP_KEEP_DAYS:-7}
BACKUP_HOUR_UTC=${BACKUP_HOUR_UTC:-21}   # 21:00 UTC = 02:30 IST

dump() {
    file="$BACKUP_DIR/nool-$(date -u +%Y%m%dT%H%M%SZ).dump"
    # Write to .partial first so a failed/killed dump never looks like a backup.
    pg_dump --format=custom --file="$file.partial"
    mv "$file.partial" "$file"
    echo "backup: wrote $file ($(du -h "$file" | cut -f1))"
    # -mtime +N means "older than N+1 whole days", so keep N days = +(N-1).
    find "$BACKUP_DIR" -name 'nool-*.dump' -mtime +$((BACKUP_KEEP_DAYS - 1)) -print -delete
}

# Seconds until the next BACKUP_HOUR_UTC:00 - plain epoch arithmetic, since
# busybox date has no "tomorrow 02:00" parsing.
seconds_until_next() {
    now=$(date -u +%s)
    target=$(( now - now % 86400 + BACKUP_HOUR_UTC * 3600 ))
    [ "$target" -le "$now" ] && target=$(( target + 86400 ))
    echo $(( target - now ))
}

mkdir -p "$BACKUP_DIR"
if [ "${1:-}" = once ]; then
    dump
    exit 0
fi
while true; do
    wait_s=$(seconds_until_next)
    echo "backup: next dump in ${wait_s}s"
    sleep "$wait_s"
    # A failed dump must not kill the loop (and skip every later night).
    dump || echo "backup: FAILED at $(date -u)" >&2
done
