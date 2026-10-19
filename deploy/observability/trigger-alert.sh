#!/usr/bin/env bash
# Fire the alert path on demand, to prove that an alert wakes the on-call agent.
#
#   trigger-alert.sh dispatch   send the payload Grafana would send straight to
#                               GitHub (needs GITHUB_REPO and GITHUB_TOKEN)
#   trigger-alert.sh relay      post a firing Grafana notification to the alert relay
#                               (RELAY_URL, default http://localhost:8080/; on the VM:
#                               docker compose exec oncall-relay python -c ... or
#                               run it from a container on the compose network)
#
# Either way the "On-call agent" workflow starts within seconds.
set -euo pipefail

mode="${1:-dispatch}"
title="${ALERT_TITLE:-Drill: Users are getting server errors}"
summary="${ALERT_SUMMARY:-Drill: more than 5% of web requests answered 5xx}"

case "$mode" in
dispatch)
    : "${GITHUB_REPO:?GITHUB_REPO is required, like owner/repo}" "${GITHUB_TOKEN:?GITHUB_TOKEN is required}"
    curl -fsS -X POST "https://api.github.com/repos/$GITHUB_REPO/dispatches" \
        -H "Authorization: Bearer $GITHUB_TOKEN" -H "Accept: application/vnd.github+json" \
        -d "{\"event_type\":\"grafana-alert\",\"client_payload\":{\"title\":\"$title\",\"status\":\"firing\",\"summary\":\"$summary\"}}"
    echo "dispatched: watch Actions > On-call agent"
    ;;
relay)
    curl -fsS -X POST "${RELAY_URL:-http://localhost:8080/}" -H "Content-Type: application/json" \
        -d "{\"status\":\"firing\",\"commonLabels\":{\"alertname\":\"$title\"},\"commonAnnotations\":{\"summary\":\"$summary\"}}"
    echo "sent to the relay: watch Actions > On-call agent"
    ;;
*)
    echo "usage: $0 dispatch|relay" >&2
    exit 2
    ;;
esac
