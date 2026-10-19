#!/usr/bin/env bash
# Collect what the responder agent needs into one text file: the alert, and the
# latest error logs from Loki when GRAFANA_URL is reachable. Never fails the run.
# usage: fetch-context.sh <alert title> <summary> > incident-context.txt
set -uo pipefail

echo "Alert: ${1:-unknown}"
echo "Summary: ${2:-none}"
echo "Time (UTC): $(date -u +%FT%TZ)"
if [ -z "${GRAFANA_URL:-}" ]; then
    echo "Telemetry: GRAFANA_URL is not set, no logs were fetched."
    exit 0
fi
echo
echo "Error logs of the last 15 minutes (JSON lines carry trace_id):"
curl -fsS --max-time 20 -G "$GRAFANA_URL/api/datasources/proxy/uid/loki/loki/api/v1/query_range" \
    ${GRAFANA_TOKEN:+-H "Authorization: Bearer $GRAFANA_TOKEN"} \
    --data-urlencode 'query={service_name=~"web|worker"} | json | level=~"ERROR|WARNING"' \
    --data-urlencode "start=$(($(date +%s) - 900))000000000" \
    --data-urlencode 'limit=50' \
    | python3 -c '
import json, sys
for stream in json.load(sys.stdin)["data"]["result"]:
    for _, line in stream["values"]:
        print(line[:600])
' || echo "(logs could not be fetched)"
exit 0
