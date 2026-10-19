# Testing guidelines

## Running

- `uv run pytest` runs everything; `uv run pytest tests/test_<area>.py -k name` runs one test.
- Tests use `config/settings_test.py` (fixed, non-secret values). A local PostgreSQL must be running; pytest-django
  creates its own `test_` database. In CI it is a service container.
- Markers: `benchmark` (20-class solver regression guard, in the plain suite) and `benchmark_full` (40 classes,
  about a minute, excluded by default; run with `-m benchmark_full`).
- Lint is part of done: `uv run ruff check . && uv run ruff format --check .`.

## Two levels

- **Unit**: pure functions and rules, no database where possible: the solver model, `resolve_hours`, hours target, CSV
  and PDF parsers, permission predicates, redaction. Fast, one behaviour per test.
- **Integration**: the Django test client against the real PostgreSQL: login, then a view, then database state and the
  rendered response. Every screen and every mutation has at least one; every mutation also has one for each role
  that must be refused (`SEGRETERIA` on admin actions, anonymous) and one for a stale `version`.

## Conventions

- One file per area, named after what it covers (`test_move_lesson.py`, `test_availability_link_page.py`).
- Shared fixtures live in `tests/conftest.py` (`admin`, `seg`, school and timetable builders). Build the minimum data a
  test needs; do not depend on test order.
- Assert on behaviour (status, redirect, rows written, text shown), not on markup details.
- A bug fix starts with a test that fails for the reported reason.
- Authorization: when a predicate is added to `projects/permissions.py`, add it to the matrix in
  `tests/test_permissions.py`; that file also fails if a view checks roles inline.
- Time and randomness are passed in or frozen; no test depends on today's date.
- Anything that calls the Anthropic API is mocked at the `ai/` boundary. No test touches the network.
- Secrets and tokens: assert they are absent from logs, traces and responses where the rules say so
  (`tests/test_telemetry.py`, `tests/test_availability_link_page.py`).
- `tests/test_end_to_end.py` is the full journey (login, setup, magic link, generation, approval, export, role checks); extend it when a step of that journey changes.
- Solver tests use crafted small weeks with a known answer; the benchmark guards the timeout on a school-sized week.

## Before closing a task

The whole suite passes, ruff is clean, every acceptance criterion has a test or a stated manual check, and new settings
are in `.env.example`.
