# HTTP surface

There is no JSON API. Every endpoint returns HTML: a full page, or a fragment that HTMX swaps into a page. All of it
requires login (role `ADMIN` or `SEGRETERIA`) except the bearer-token routes below. Authorization is the predicate named
in `projects/permissions.py`; `specs.md` §4 has the matrix. The source of truth for paths is each app's `urls.py`.

## Conventions

- Mutations are `POST` with the CSRF token (HTMX sends it from `<body hx-headers>`).
- Timetable mutations carry `version`, the value of `Timetable.version` the user saw. A stale value is refused with a
  message and the current grid; a successful one bumps the counter and returns the full re-rendered grid.
- `GET /orario/<week>/griglia/?versione=<n>` is the poll: `204` when the viewer already has version `n`, the grid otherwise.
- `<week>` is the Monday as `YYYY-MM-DD`.
- Validation errors re-render the form with field errors (HTTP 200); a forbidden action is `403`; throttled lookups `429`
  with `Retry-After`.
- Background work: `POST` enqueues a `SolveJob` and returns a status fragment; `GET /orario/<week>/stato/` is polled
  until the job is `DONE` or `FAILED`.

## Routes by area

| Area | Paths | Notes |
|---|---|---|
| Accounts | `/login/`, `/logout/`, `/password/` | Change password needs the current one |
| Overview | `/`, `/partials/ping/` | Backup status for admins |
| Setup | `/setup/...` | Upload, validate, correct and save the setup files; admin only |
| School data | `/dati-scuola/...` | Teachers, classes, subjects, assignments, pairings, calendar; read for both roles, edit admin |
| Availability | `/disponibilita/`, `/disponibilita/link/` | Grids edited by staff; link management |
| Teacher link page | `/l/<token>/`, `/l/<token>/salva/` | Bearer token, no login, rate-limited, never logged in full |
| Timetable | `/orario/`, `/orario/<week>/...` | Grid, move, assign, generate, conflicts, quick actions, repair, alternatives, approve, export PDF/Excel, job status |
| Soft weights | `/pesi/...` | Admin |
| Audit | `/audit/` | Admin |
| Feeds | `/feeds/`, `/feeds/<token>.ics` | iCal per class or teacher; token in path |
| Public week | `/orario/<token>/` | Read-only approved week of a class, off unless `PUBLIC_WEEK_PAGE_ENABLED` |
| Admin | `/admin/` | Django admin, for debugging |

## Credentials in paths

`/l/<token>/`, `/feeds/<token>.ics` and `/orario/<token>/` carry a bearer token. They are rate-limited by client address,
excluded from the gunicorn access log, and redacted by `availability.logging.redact` everywhere they could be printed
(logs, traces, metrics).
