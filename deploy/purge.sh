#!/usr/bin/env bash
# Runs every registered purge (parked setup uploads, dead availability links).
# Arguments such as --dry-run are passed through; the exit code is the command's.
set -euo pipefail
# shellcheck source=deploy/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
cd "$ROOT"

dc exec -T web python manage.py run_scheduled_purges "$@"
