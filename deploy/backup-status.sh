#!/usr/bin/env bash
# Age of the last successful backup, read from BACKUP_DIR/last-success (written by backup.sh).
# Exit 0 fresh, 1 stale, 2 no valid marker or bad configuration.
set -euo pipefail
# shellcheck source=deploy/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
cd "$ROOT"

threshold="$(setting BACKUP_STALE_HOURS 30)"
if ! [[ "$threshold" =~ ^[1-9][0-9]*$ ]]; then
    echo "BACKUP_STALE_HOURS must be a positive integer, got '$threshold'." >&2
    exit 2
fi

dir="$(backup_dir)"
marker="$dir/last-success"
invalid() { echo "no valid backup marker in $dir" >&2; exit 2; }

[ -f "$marker" ] || invalid
line="$(head -n 1 "$marker" 2>/dev/null || true)"
re='^([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z) (gestione-docenti-[^ /]+\.dump)$'
[[ "$line" =~ $re ]] || invalid
stamp="${BASH_REMATCH[1]}"
dump="${BASH_REMATCH[2]}"
then_s="$(date -u -d "$stamp" +%s 2>/dev/null)" || invalid
now_s="$(date +%s)"
[ "$then_s" -le "$now_s" ] || invalid

hours=$(((now_s - then_s) / 3600))
if [ "$hours" -gt "$threshold" ]; then
    echo "last backup: $hours hours ago, STALE (threshold $threshold h)"
    exit 1
fi
if [ "$hours" -lt 1 ]; then
    ago="less than 1 hour ago"
elif [ "$hours" -eq 1 ]; then
    ago="1 hour ago"
else
    ago="$hours hours ago"
fi
echo "last backup: $ago ($dump)"
