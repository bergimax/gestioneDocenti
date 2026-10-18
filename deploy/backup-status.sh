#!/usr/bin/env bash
# Age of the last successful backup, read from BACKUP_DIR/last-success (written by backup.sh).
# When OFFSITE_RCLONE_REMOTE is set, also the age of the last off-site copy, read from
# BACKUP_DIR/last-offsite-success (written by backup-offsite.sh), on a second line.
# Exit 0 fresh, 1 stale, 2 no valid marker or bad configuration; with two checks, the worst.
set -euo pipefail
# shellcheck source=deploy/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
cd "$ROOT"

threshold="$(setting BACKUP_STALE_HOURS 30)"
if ! [[ "$threshold" =~ ^[1-9][0-9]*$ ]]; then
    echo "BACKUP_STALE_HOURS must be a positive integer, got '$threshold'." >&2
    exit 2
fi
offsite_threshold="$(setting OFFSITE_STALE_HOURS 192)"
if ! [[ "$offsite_threshold" =~ ^[1-9][0-9]*$ ]]; then
    echo "OFFSITE_STALE_HOURS must be a positive integer, got '$offsite_threshold'." >&2
    exit 2
fi

dir="$(backup_dir)"

# check MARKER_NAME INVALID_WORD LINE_LABEL THRESHOLD SINGLE_LINE: print the age line, return 0/1/2.
# SINGLE_LINE=1 (off-site marker only) also rejects a marker with more than one line.
check() {
    local marker="$dir/$1" word="$2" label="$3" limit="$4" single="$5"
    local line stamp dump then_s now_s hours ago
    local re='^([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z) (gestione-docenti-[^ /]+\.dump)$'
    if [ -f "$marker" ]; then
        line="$(head -n 1 "$marker" 2> /dev/null || true)"
    else
        line=""
    fi
    if [ -f "$marker" ] && [[ "$line" =~ $re ]] \
        && { [ "$single" != 1 ] || [ "$(wc -l < "$marker" 2> /dev/null || echo 0)" -le 1 ]; }; then
        stamp="${BASH_REMATCH[1]}"
        dump="${BASH_REMATCH[2]}"
        if then_s="$(date -u -d "$stamp" +%s 2> /dev/null)"; then
            now_s="$(date +%s)"
            if [ "$then_s" -le "$now_s" ]; then
                hours=$(((now_s - then_s) / 3600))
                if [ "$hours" -gt "$limit" ]; then
                    echo "$label: $hours hours ago, STALE (threshold $limit h)"
                    return 1
                fi
                if [ "$hours" -lt 1 ]; then
                    ago="less than 1 hour ago"
                elif [ "$hours" -eq 1 ]; then
                    ago="1 hour ago"
                else
                    ago="$hours hours ago"
                fi
                echo "$label: $ago ($dump)"
                return 0
            fi
        fi
    fi
    echo "no valid $word marker in $dir" >&2
    return 2
}

rc=0
check last-success backup "last backup" "$threshold" 0 || rc=$?
if [ -n "$(setting OFFSITE_RCLONE_REMOTE)" ]; then
    off=0
    check last-offsite-success off-site "last off-site copy" "$offsite_threshold" 1 || off=$?
    [ "$off" -le "$rc" ] || rc="$off"
fi
exit "$rc"
