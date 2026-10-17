# Gestione docenti

## How to run

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
solve and exits cleanly. The solver timeout is `SOLVER_TIMEOUT_SECONDS`.

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
