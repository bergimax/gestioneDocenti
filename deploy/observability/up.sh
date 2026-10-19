#!/usr/bin/env bash
# Start the production stack with Grafana/Tempo/Loki/Prometheus next to it.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/../lib.sh"
cd "$ROOT"
check_env
dc -f docker-compose.yml -f deploy/observability/otel.yml up -d --build
dc -f docker-compose.yml -f deploy/observability/otel.yml ps
