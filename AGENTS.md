# Gestione docenti

Weekly timetable generation for a vocational school: a CP-SAT solver builds the week, an admin reviews, fixes and
approves it, and the result is exported. Django monolith, PostgreSQL, HTMX. Read the README for the problem statement.

## Where things are

- `_docs/specs.md` - scope and domain rules (constraints, weights, calendar resolution, permission matrix)
- `_docs/architecture.md` - how it is built
- `_docs/tasks.md` - the original build order; live work is GitHub issues
- `_docs/process.md` - how work is organized (PM, engineer, QA); roles in `_docs/team/`
- `_docs/task-template.md` - the shape of a groomed issue
- `_docs/testing-guidelines.md` - read before writing tests
- `_docs/design-system.md` - read before touching templates or CSS
- `_docs/api.md` - the HTTP surface (server-rendered; no JSON API)
- `deploy/README.md`, `deploy/observability/README.md` - production, backups, telemetry, on-call agent

## Commands

- `uv sync` - install dependencies
- `uv run manage.py runserver` - dev server (needs PostgreSQL and the env, see `.env.example`)
- `uv run manage.py solve_worker` - the background worker that runs generation jobs
- `uv run manage.py migrate` - apply migrations
- `uv run pytest` - the whole suite (needs PostgreSQL on localhost, see README)
- `uv run pytest tests/test_home.py` - one file
- `uv run ruff check . && uv run ruff format --check .` - run before every commit
- `deploy/up.sh` - the whole stack in Docker

## Layout

`config/` settings and telemetry; `projects/` permissions and navigation; `accounts/` users; `school/` master data,
setup import, calendar; `availability/` grids and magic links; `scheduling/` timetable, jobs, conflicts, approval;
`scheduling/solver/` the framework-free CP-SAT model; `ledger/` hours ledger; `export/` PDF and Excel; `feeds/` iCal
and the public week page; `ai/` Anthropic calls; `templates/`, `static/`, `tests/`.
`frontend/` is an unused Lovable prototype of the UI: do not build on it, do not run it in production.

## Rules

- All authorization lives in `projects/permissions.py` as predicate functions taking a user and a domain object. Views
  use `permission_required` or `PredicatePermissionMixin`; never an inline `request.user.role ==` check. The UI may hide
  what a role cannot do, the server always enforces it.
- Approval goes through `approve_timetable()` only: forward-only, admin-only, with its ledger entries and audit
  entry in the same transaction as the state write.
- Every timetable mutation runs in one transaction, bumps `Timetable.version` and returns the full grid. Open pages poll
  that counter. No WebSockets, no diffs. A stale `version` in a POST is refused.
- Magic links, feed tokens and public-page tokens are bearer credentials: stored hashed where the model says so,
  rate-limited, and never logged or traced in full. Use `availability.logging.redact` for anything that prints a path.
- No customer data (names, emails, request bodies, query strings) in logs, traces or metrics.
- The solver (`scheduling/solver/`) imports no Django. It takes dataclasses and returns dataclasses.
- `ai/` holds no models and no views: functions that take domain objects and return plain dicts, so every Anthropic call
  is mockable. AI output is a draft and is never applied until an admin confirms it. Tests never call the network.
- PostgreSQL is the only infrastructure. No Redis, no Celery, no object store, no mail backend. Background jobs are
  `django.tasks` with the `django-tasks-db` backend.
- There is no email: no verification, no password reset. A forgotten password is reset with
  `manage.py changepassword <email>`.
- Pages are Django templates with HTMX and Alpine, vendored in `static/vendor/`. No build step, no Node.
- Configuration comes from the environment. A new setting is a new env var plus an entry in `.env.example` (and
  `deploy/env.production.example` if production needs it), never a hardcoded value or a checked-in secret.
- Tests live in `tests/`, named after what they cover. `config/settings_test.py` supplies the test environment;
  production settings stay strict.
- Dependencies are pinned exactly in `pyproject.toml`; run `uv sync` so `uv.lock` moves with it. Do not add a
  dependency without asking.
- Do not edit `_docs/` as a side effect of building something. Changing the design is its own deliberate change.
- UI text is Italian (the users are Italian); code, comments, docs, commits and issues are English.
- Commit regularly. One task per commit series; the commit message says what and why.
