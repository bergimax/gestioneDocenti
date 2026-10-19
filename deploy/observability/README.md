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

No CPU, memory or disk alerts. All three post to the GitHub `repository_dispatch` API.

Set in `.env.production`:

- `ONCALL_WEBHOOK_URL=https://api.github.com/repos/<owner>/<repo>/dispatches`
- `ONCALL_WEBHOOK_TOKEN=<fine-grained token, Contents: read and write on this repo>`

## On-call agent

`.github/workflows/on-call.yml` wakes on that dispatch (or manually, for a
drill: Actions > On-call agent > Run workflow). It opens an incident issue, runs
Claude Code against the repo with the alert as context, and opens a pull
request with a failing test and the fix. It never deploys: CI and a human merge
come first. Needs the repository secret `ANTHROPIC_API_KEY`.
