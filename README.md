# Gestione docenti

## The problem

A vocational training school (IeFP / upper-secondary courses) builds a weekly timetable by hand every week: about 20
classes, dozens of teachers (some always available, some only in the hours they declare), yearly hour targets per
teacher-class-subject, paired classes, stages (classes out of school) and a yearly calendar of closures. Doing this in
spreadsheets is slow, error-prone and hard to review.

## What it does

- **Set-up from files**: school data, classes, teachers, subjects, assignments and the yearly calendar are imported
  from CSV/PDF and validated, or edited on data pages.
- **Availability**: contract teachers declare their available hours through a personal link; permanent teachers are
  always available.
- **Generation**: a constraint solver (OR-Tools CP-SAT) builds one week at a time, running in a background worker so the
  web page stays responsive. It respects the calendar, hour targets, pairings and availability.
- **Review**: the admin sees conflicts with plain-language explanations, moves or reassigns lessons, applies explicit
  one-off exceptions and approves the week.
- **Publish**: approved weeks are exported as PDF/Excel, shown on a public read-only page and fed to calendar apps.
- **Roles**: `ADMIN` and `SEGRETERIA`; no email, no self-registration. Every sensitive action is audited.

Expected behaviour: given consistent input data, a generated week has no teacher or class double-booked, honours
availability and the weekly hours target, and nothing is published until an admin approves it.

Scope and rules: [_docs/specs.md](_docs/specs.md). Design: [_docs/architecture.md](_docs/architecture.md). Task list
(GitHub issues, each with acceptance criteria): <https://github.com/bergimax/gestioneDocenti/issues>. How the AI agent
team works: [_docs/process.md](_docs/process.md) and [_docs/team/](_docs/team/). Agent entry point: [AGENTS.md](AGENTS.md).

## How to run

With Docker (database, web, worker and proxy, one command; see [deploy/README.md](deploy/README.md)):

    deploy/up.sh

Locally:

    uv sync
    uv run manage.py migrate
    uv run manage.py runserver   # http://127.0.0.1:8000/
    uv run pytest

## Lint and format

    uv run ruff check .
    uv run ruff format --check .

Run both before committing. CI (`.github/workflows/ci.yml`) runs the same commands plus `uv run pytest` on every push and pull request.

## Configuration

Settings come from environment variables; see `.env.example` for the full list
(`SECRET_KEY`, `DEBUG`, `ALLOWED_HOSTS`, `DATABASE_URL`, `SOLVER_TIMEOUT_SECONDS`).
Export them in the shell, or copy `.env.example` to `.env` (git-ignored) and run

    uv run --env-file .env manage.py runserver

A missing or malformed value stops startup with an error naming the variable.

Tests use `config/settings_test.py`, which supplies fixed non-secret values, so
nothing needs exporting. Its `DATABASE_URL` defaults to
`postgres://postgres:postgres@localhost:5432/gestione_docenti`; a `DATABASE_URL`
exported in the shell overrides it. A local PostgreSQL must be running for tests
that touch the database (pytest-django creates its own `test_` database).

## Solve worker

Timetable generation runs outside the web process, queued in PostgreSQL with
`django.tasks` (`django-tasks-db`). Start the worker next to the web server:

    uv run manage.py migrate
    uv run manage.py solve_worker

It takes the options of `db_worker` (`--batch` processes the pending jobs and
exits, `--max-tasks 1` stops after one job, `--interval N` sets the polling
seconds). One process runs one solve at a time; Ctrl-C finishes the running
solve and exits cleanly. The solver timeout is `SOLVER_TIMEOUT_SECONDS`. A job left
RUNNING by a killed worker is marked failed once it is older than the timeout
plus `SOLVER_STALE_MARGIN_SECONDS` (default 120); this runs when the worker
starts, on each status poll and before a new job is enqueued.

## Users and passwords

There is no self-registration, no email and no self-service password reset.
Users are created by an operator, with a role (`ADMIN` or `SEGRETERIA`):

    uv run manage.py create_user mario@example.com --role SEGRETERIA

The password is asked interactively (hidden) unless `--password` is given; it
must be at least 8 characters. Running it for an existing email resets that
user's password and role.

A forgotten password is reset by an operator with

    uv run manage.py changepassword <email>

## Deploy

Production containers, proxy and scripts: see [deploy/README.md](deploy/README.md).

## Docker

    cp deploy/env.production.example .env.production   # fill in the four required values
    deploy/up.sh

Starts PostgreSQL, web (gunicorn), the solve worker and Caddy (HTTPS) with one command.

## CI/CD

- `ci.yml`: ruff and the full test suite (unit and integration, against PostgreSQL) on every push and pull request.
- `deploy.yml`: a commit on `main` that passed CI is deployed to the **dev** environment automatically over SSH
  (`deploy/release.sh`); **production** is deployed only by running the workflow by hand, behind the required
  reviewers of the `production` environment. Rollback: run it again with the previous commit.
  Secrets per environment: `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_SSH_KEY`, `DEPLOY_KNOWN_HOSTS`, `DEPLOY_PATH`.

## Observability, alerting and on-call agent

OpenTelemetry traces, metrics and JSON logs sharing a `trace_id`, viewable in Grafana; an alert on the 5xx rate; and an
agent that wakes on that alert and opens a fix pull request. See
[deploy/observability/README.md](deploy/observability/README.md).
