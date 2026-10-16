#!/usr/bin/env bash
# First start. Safe to run again: it never removes volumes.
set -euo pipefail
# shellcheck source=deploy/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
cd "$ROOT"

check_env
deploy_stack
dc ps
