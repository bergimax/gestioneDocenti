#!/usr/bin/env bash
# Nightly backup: pg_dump (custom format) of the database into BACKUP_DIR, then prune old dumps.
set -euo pipefail
# shellcheck source=deploy/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
cd "$ROOT"

log() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
fail() { log "backup FAILED: $*" >&2; exit 1; }

keep="$(setting BACKUP_KEEP 14)"
if ! [[ "$keep" =~ ^[1-9][0-9]*$ ]]; then
    fail "BACKUP_KEEP must be a positive integer, got '$keep'. Nothing was dumped or deleted."
fi

dir="$(backup_dir)"
mkdir -p "$dir" 2>/dev/null || fail "cannot create the backup directory $dir"

name="gestione-docenti-$(date +%Y%m%d-%H%M%S)-$$-$RANDOM.dump"
final="$dir/$name"
partial="$final.partial"
marker="$dir/last-success"
marker_tmp="$dir/.last-success.tmp.$$"
trap 'rm -f "$partial" "$marker_tmp"' EXIT

: > "$partial" 2>/dev/null || fail "cannot write in the backup directory $dir (read-only or full?)"

pg_exec pg_dump --format=custom -U "$PG_USER" -d "$PG_DB" > "$partial" \
    || fail "pg_dump did not complete: is the postgres container running, and are POSTGRES_USER/POSTGRES_DB right? (disk full is another cause)"
[ -s "$partial" ] || fail "pg_dump produced an empty file"
pg_exec pg_restore --list < "$partial" > /dev/null \
    || fail "the dump failed verification (pg_restore --list)"
mv "$partial" "$final" || fail "cannot move the dump into place in $dir"

log "backup written: $final ($(wc -c < "$final") bytes)"

# Marker read by backup-status.sh and the overview page: written only on success, atomically.
printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$name" > "$marker_tmp" \
    && mv -f "$marker_tmp" "$marker" \
    || fail "the dump is in place but the marker $marker could not be written"

# Retention: keep the newest KEEP dumps, the one just written always among them.
removed=0
while IFS= read -r old; do
    [ -n "$old" ] || continue
    [ "$old" = "$final" ] && continue
    rm -f -- "$old"
    removed=$((removed + 1))
done < <(list_dumps "$dir" | grep -vxF "$final" | tail -n +"$keep")
log "retention: keeping $keep, removed $removed"
