#!/usr/bin/env bash
# Deploy an exact git ref (branch, tag or commit). Used by the CD workflow, and
# by hand for a rollback: run it with the previous commit printed in the last run.
# Migrations only go forward: a rollback to code older than an applied migration
# needs a restore (deploy/restore.sh) if that migration is not backward compatible.
set -euo pipefail
# shellcheck source=deploy/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
cd "$ROOT"

ref="${1:?usage: release.sh <git ref>}"
echo "previous release: $(git rev-parse HEAD)"
git fetch --tags origin
git checkout --detach "$ref"
check_env
deploy_stack
docker image prune -f
dc ps
echo "deployed: $(git rev-parse HEAD)"
