#!/usr/bin/env bash
# Update: pull, rebuild, migrate, restart. Stops before touching the stack if the pull fails.
set -euo pipefail
# shellcheck source=deploy/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
cd "$ROOT"

git pull --ff-only
check_env
deploy_stack
docker image prune -f
dc ps
