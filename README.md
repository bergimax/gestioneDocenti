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
