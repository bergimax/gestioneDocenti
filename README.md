# Gestione docenti

## How to run

    uv sync
    uv run manage.py migrate
    uv run manage.py runserver   # http://127.0.0.1:8000/
    uv run pytest

## Lint and format

    uv run ruff check .
    uv run ruff format --check .

Run both before committing.

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
