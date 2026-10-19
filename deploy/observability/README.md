# Observability, alerting and on-call agent

## Start

    deploy/observability/up.sh

Starts the production stack plus one `grafana/otel-lgtm` container (Grafana,
Tempo traces, Loki logs, Prometheus metrics). Set `GRAFANA_ADMIN_PASSWORD` in
`.env.production`. Grafana listens on 127.0.0.1:3000: open it through an SSH
tunnel (`ssh -L 3000:localhost:3000 <vm>`). `web` and `worker` export over
OTLP/HTTP because the overlay sets `OTEL_EXPORTER_OTLP_ENDPOINT`; without that
variable telemetry is off and logs stay JSON on stdout.

## What is recorded

Key user journey: **generate a week's timetable** (login, request, background solve).

- Traces (Grafana > Explore > Tempo): every HTTP request (`GET home`), SQL
  statements, and the `timetable.generate` span of the worker job.
- Metrics (Prometheus): `http_server_request_duration_seconds_*` by status code,
  `timetable_generations_total{outcome}` and `timetable_generation_duration_seconds`.
- Logs (Grafana > Explore > Loki, or `docker compose logs`): one JSON line per
  record with `trace_id` and `span_id`, the same ids as the trace. Search a
  `trace_id` in Loki to see every log line of a request.

No customer data: request paths are redacted (magic-link tokens keep at most 6
characters), query strings are dropped, there are no request bodies, user names
or emails in spans or logs (`config/telemetry.py`, `tests/test_telemetry.py`).

## Alerts (user impact, not resource graphs)

`grafana/provisioning/alerting/alerts.yml` provisions three alerts, each one something a user feels:

| Alert | Fires when |
|---|---|
| Users are getting server errors | more than 5% of requests answer 5xx over 5 minutes (2 min) |
| Pages are slow for users | p95 response time above 3 s over 10 minutes (5 min) |
| Timetable generation is crashing | a generation job ended in `outcome="error"`, an unexpected exception. An infeasible week or a timeout is `failed`: bad data, not an incident |

No CPU, memory or disk alerts.

## From alert to agent

    Grafana alert --webhook--> oncall-relay --repository_dispatch--> GitHub Actions "On-call agent"

Grafana cannot send the body GitHub requires, so `relay.py` (stdlib only, a container of the overlay, reachable only
on the compose network) turns a firing notification into a `repository_dispatch` and drops resolved ones. It forwards
the alert name and summary only. Verified end to end: a firing rule reaches the relay and the relay sends
`{"event_type": "grafana-alert", "client_payload": {...}}`.

Set in `.env.production`: `GITHUB_REPO=<owner>/<repo>` and `ONCALL_WEBHOOK_TOKEN=<fine-grained token, Contents: read
and write on this repo>`.

## Trigger the alert on demand

    GITHUB_REPO=owner/repo GITHUB_TOKEN=... deploy/observability/trigger-alert.sh dispatch   # straight to GitHub
    RELAY_URL=http://oncall-relay:8080/ deploy/observability/trigger-alert.sh relay           # through the relay

Either starts the On-call agent workflow within seconds (Actions > On-call agent), also startable by hand with
"Run workflow". A real trigger for "Timetable generation is crashing" exists today: issue #172 (generate after a setup
with a non-empty pairings CSV).

## On-call agent

`.github/workflows/on-call.yml` wakes on the dispatch. It opens an incident issue, collects context
(`fetch-context.sh`: the alert and, if the repository secrets `GRAFANA_URL` and `GRAFANA_TOKEN` are set, the last 15
minutes of error logs with their `trace_id`), then runs Claude Code under the role in
[_docs/team/on-call.md](../../_docs/team/on-call.md): reproduce with a failing test, fix, run lint and the suite, open a
pull request. It never deploys: CI and a human merge come first. Needs one repository secret: `CLAUDE_CODE_OAUTH_TOKEN` (Pro/Max subscription, from `claude setup-token`) or `ANTHROPIC_API_KEY`.
