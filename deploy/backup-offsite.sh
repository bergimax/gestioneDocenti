#!/usr/bin/env bash
# Weekly off-site copy of the newest dump with rclone. Does nothing unless OFFSITE_RCLONE_REMOTE is set.
set -euo pipefail
# shellcheck source=deploy/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
cd "$ROOT"

remote="$(setting OFFSITE_RCLONE_REMOTE)"
if [ -z "$remote" ]; then
    echo "off-site not configured, skipped"
    exit 0
fi

fail() { echo "off-site copy FAILED: $*" >&2; exit 1; }

command -v rclone > /dev/null || fail "rclone is not installed on this host"

# rclone reads its credentials from the environment: pass on what the env file defines
# (never on the command line, where ps would show it).
config="$(setting OFFSITE_RCLONE_CONFIG)"
[ -z "$config" ] || export RCLONE_CONFIG="$config"
if [ -f "$ENV_FILE" ]; then
    while IFS= read -r var; do
        [ -n "${!var:-}" ] || export "$var=$(env_value "$var")"
    done < <(grep -oE '^RCLONE_CONFIG_[A-Za-z0-9_]+' "$ENV_FILE" || true)
fi

dir="$(backup_dir)"
newest="$(list_dumps "$dir" 2> /dev/null | head -n 1 || true)"
[ -n "$newest" ] || fail "no dump found in $dir: run deploy/backup.sh first"

marker="$dir/last-offsite-success"
marker_tmp="$dir/.last-offsite-success.tmp.$$"
trap 'rm -f "$marker_tmp"' EXIT

rclone copyto "$newest" "${remote%/}/$(basename "$newest")" \
    || fail "rclone could not copy $(basename "$newest") to the destination"
# Only now, after rclone reported success: record it (atomically; -T so a directory in the way fails).
printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$(basename "$newest")" > "$marker_tmp" \
    && mv -fT "$marker_tmp" "$marker" \
    || fail "the copy succeeded but the marker $marker could not be written"
echo "$(date '+%Y-%m-%d %H:%M:%S') off-site copy done: $(basename "$newest")"
