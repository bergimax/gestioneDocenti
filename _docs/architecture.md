# Architecture

Design for the rebuild of the school timetable platform described in `specs.md`. `specs.md` stays the scope and the
source of the domain rules (constraints, weights, calendar resolution); this document says how the system is built.
`tasks.md` holds the build order.

## 1. Principles

1. **The solver is the product.** Effort goes into making the CP-SAT model more useful (explainable, repairable);
   everything around it is commodity and is kept as cheap as possible.
2. **One language, one process family, one database.** Python end to end, Django monolith, Postgres as the only
   infrastructure. No Node, Redis, Celery, object store or mail backend.
3. **The server is the single source of truth.** The UI never holds timetable state that the server has not validated.
4. **AI proposes, people decide.** Every LLM output lands as a draft and is inert until an admin confirms it.
5. **Runs free.** Target: the Oracle Always Free VM (1 GB RAM), about 400 MB resident, no recurring cost beyond cents of
   optional LLM usage.

## 2. System overview

```
Browser ──HTTPS──> Caddy ──> gunicorn (Django, 2 workers) ──> PostgreSQL
                                   │                              ▲
                                   └── enqueue job ──> solver worker (django.tasks, DB backend)
```

| Component | Choice | Notes |
|---|---|---|
| Web app | Django, gunicorn | Auth, roles, forms, migrations, admin come from the framework |
| UI | Django templates, HTMX, Alpine | No build step, no Node |
| Timetable grid | HTMX + SortableJS drag and drop | Each move is a server call that validates and returns the re-rendered grid |
| Database | PostgreSQL | Also the job queue (`django-tasks-db`) |
| Solver | `ortools` CP-SAT in a separate worker process | Isolated memory and crashes; killed on timeout |
| PDF / Excel | WeasyPrint, openpyxl | Ported from the current implementation |
| PDF input | pdfplumber, optional vision-model reader | See §9 |
| Reverse proxy | Caddy | Automatic HTTPS |
| LLM (optional) | Anthropic API, small model | Draft-only, can be disabled by leaving the key unset |

A React island is permitted only for the timetable grid, and only if HTMX proves inadequate for it. Every other screen
is templates with HTMX.

## 3. Repository layout

```
config/            settings (env-driven), settings_test.py, urls, wsgi
accounts/          users, roles, magic-link tokens
school/            school, classes, teachers, subjects, assignments, pairings, calendar
availability/      weekly availability grids, magic-link entry pages
scheduling/        timetable, slots, jobs, conflicts, advance/approve flow, feedback
  solver/          pure-Python CP-SAT model: no Django imports, plain dataclasses in and out
ledger/            append-only hours ledger
exports/           PDF, Excel, iCalendar feeds
ai/                no models, no views: functions taking domain objects, returning plain dicts
templates/ static/
tests/             named after what they cover
```

`scheduling/solver/` is deliberately framework-free so it can be tested, benchmarked and run in the worker without
Django state. The existing solver, parsers and validators are ported into it and `ai/`, keeping their tests.

## 4. Data model

Carried over from `specs.md` §3 (`scuola`, `classe`, `docente`, `materia`, `assegnazione`, `calendario_annuale`,
`classe_accoppiata`, `orario_settimanale`, `slot_lezione`, `audit_log`, `orario_feedback`, `soft_weight_adjustment`),
now as Django models with real migrations, with these changes:

| Change | Reason |
|---|---|
| `ore_erogate` is replaced by **`LedgerEntry`** (assignment, week, hours, kind, created_at, schedule FK) | Append-only; the delivered hours are the sum of entries. Approval writes entries; undoing an approval writes compensating entries. |
| `admin`/`admin_session` replaced by Django `User` with a `role` (`ADMIN`, `SEGRETERIA`) and Django sessions | Removes hand-rolled PBKDF2 and session code |
| `AvailabilityLink` (teacher, week, token hash, expires_at, used_at) | Magic-link entry, see §7 |
| `SolveJob` (week, kind `GENERATE` / `REPAIR`, status, params, result, started_at, finished_at, error) | Job state visible to the UI |
| `Draft` (kind, payload, source, status `DRAFT` / `CONFIRMED` / `DISCARDED`) | Holds every AI-produced proposal |
| `Timetable.version` (int) | Bumped inside every transaction that changes the board, see §6 |
| single-school `sch_1` constant removed from the schema | The school is a row, not a hardcoded id; multi-tenancy stays out of scope |
| `preferenze_ai_memory` dropped | Unused in v1 |

## 5. Authorization

All authorization lives in one module of predicate functions taking a user and a domain object (for example
`can_generate(user, week)`, `can_edit_availability(user, teacher)`). Views, templates and tasks call predicates; there
are no inline role checks. The permission matrix is the one in `specs.md` §4. The UI hides what a role cannot do, but
enforcement is always server-side.

## 6. Timetable lifecycle and sync

States: absent → `BOZZA` → `APPROVATO`, transitions only through a single function, `approve_timetable()`:
forward-only, admin-only, with its side effects (ledger entries, audit log, state write) in one transaction.

Every mutation of a timetable (move, assign, conflict decision, regeneration, approval) runs in one transaction that
bumps `Timetable.version` and returns the full re-rendered grid. Open pages poll the version counter (HTMX `hx-trigger`
every few seconds) and refetch only when it changed. No WebSockets, no diffs.

## 7. Solver service

### 7.1 Execution
- `POST generate` creates a `SolveJob` and enqueues it with `django.tasks`; the request returns immediately.
- The worker builds a `ScheduleContext` from the database, runs the solver with `SOLVER_TIMEOUT_SECONDS`, and writes
  slots, quality score and conflicts in one transaction together with the version bump.
- The UI polls the job status via HTMX and swaps in the grid when done.
- The worker is a separate process, so a solver crash or an out-of-memory kill never takes the site down; the job is
  marked failed with the error.

### 7.2 Model
Unchanged from `specs.md` §9: hard constraints H1–H8, quasi-hard H4/H9/H10/H11 at the relax weight, soft preferences
S1–S5 with the stated weights, quality score and level, and the fallback chain (rigid → best-effort → forced).

### 7.3 Explainable infeasibility
Hard constraints that can make a week infeasible are modelled with assumption literals. On `INFEASIBLE`, the solver
extracts a sufficient set of failing assumptions (unsat core) and a translator turns it into Italian messages naming
the class, day, teachers and the missing capacity. The job result carries these messages plus the suggested deroghe.
If core extraction exceeds a time budget, the result falls back to the generic message.

### 7.4 Repair solving
`REPAIR` jobs take the current timetable and a perturbation (teacher absent on given days, slot removed, class
closed). The objective adds a penalty per lesson that differs from the current timetable, so only a few lessons move.
The result is shown as a diff against the current timetable and applied only on confirmation. This is the
substitutions feature that `specs.md` §17 lists as missing.

### 7.5 Alternatives and preference learning
A generation may return up to three alternatives obtained by varying the soft weights. The admin picks one; the
choice is stored as a pairwise preference and feeds the weight proposals (§8). Weight changes remain proposals that an
admin accepts or resets, each written to `soft_weight_adjustment`; nothing changes on its own.

## 8. Weight learning

Replaces star ratings with pairwise choices. For each pick, the constraint kinds on which the chosen alternative scored
better than the rejected ones count as evidence for that kind. After a threshold of consistent evidence a proposal is
created (step bounded as in `specs.md` §11: at most +50% per step, never above `MAX_SOFT_WEIGHT`). The accept and
reset flows and the append-only history are unchanged. Optional free-text feedback is kept.

## 9. AI assistance (`ai/`)

`ai/` contains no models and no views: plain functions that take domain objects and return plain dicts, so every API
call is mockable. Output is stored as a `Draft` and never applied automatically.

| Function | Input | Output draft |
|---|---|---|
| `read_calendar_pdf` | PDF file | Calendar rows (date, group, hours, closure, stage) shown in the existing preview and correction screen |
| `interpret_instruction` | Italian sentence and context | One or more deroghe or exclusions, shown as a confirmation card |

The deterministic pdfplumber parser stays as the first attempt; the vision reader is used when the parser finds nothing
or the admin asks for it. Without an API key the features are simply absent. Use the smallest model that passes the
tests; costs are expected to be cents per month.

## 10. Availability by magic link

There is no teacher login. For each teacher and week the admin generates an `AvailabilityLink`: a random token, stored
hashed, scoped to one teacher and one week, expiring after a short period, shown once as a URL to be sent by the
admin over a channel of their choice (for example WhatsApp). The page is a mobile-first tap grid that writes the same
availability record that the admin screens write. Links are bearer credentials: they are rate-limited, never logged
in full, revocable, and give no access to anything else. There is no email anywhere in the system.

## 11. Publishing

- PDF and Excel keep the layout in `specs.md` §12.
- **iCalendar feeds**: one read-only feed per teacher and per class, at an unguessable URL, covering approved weeks
  only. Subscribers see the timetable in their phone calendar; feeds regenerate from the database on request.
- A read-only public page of the approved week is optional and uses the same data.

## 12. Hours ledger

Approval writes one `LedgerEntry` per lesson (and per class for paired lessons) in the same transaction as the state
change. The remaining hours of an assignment are `ore_totali` minus the sum of its entries, and the weekly target is
computed from that residual as in `specs.md` §8. Approving twice is idempotent. Undoing an approval writes
compensating entries and returns the week to `BOZZA`. Manual corrections are entries of kind `ADJUSTMENT`.

## 13. Configuration and deployment

- Configuration comes from the environment; every new setting has an entry in `.env.example`. Production settings are
  strict; `config/settings_test.py` supplies the test environment.
- Deploy: `docker compose` with `postgres`, `web` (gunicorn), `worker` (the `django.tasks` runner) and `caddy`.
  Same VM as today; a swap file is still advisable at 1 GB.
- Backups: nightly `pg_dump` kept locally, weekly off-site copy as in the current scripts. Restore is a documented,
  tested script.
- A forgotten password is reset with `manage.py changepassword`.
- Users are created with a management command, as today; no self-registration.

## 14. Testing

- The existing solver, parser, validator and export tests are ported first and must pass before new work starts; the
  six known failing tests in `specs.md` §16 are triaged, not carried over silently.
- New coverage: unsat-core messages on crafted infeasible weeks, repair solving moves few lessons, ledger
  idempotency and undo, magic-link expiry and scope, permission predicates per role, and a solver benchmark on the real
  data to guard the timeout.
- Anything that calls an external API is mocked at the `ai/` boundary.

## 15. Risks and open points

| Risk | Mitigation |
|---|---|
| HTMX drag and drop may feel limiting | Isolate the grid behind a single endpoint contract so a React island can replace it |
| Unsat cores can be slow or large | Time budget with a generic fallback; test on real data |
| Magic links are bearer credentials | Short expiry, hashed storage, per-week scope, revocation, rate limiting |
| LLM output can be wrong | Draft-only, preview before confirm, never authoritative |
| 1 GB RAM under solver load | Separate worker, single concurrent solve, timeout, swap |
| Data migration from the running system | One-off import command from the existing Postgres, run and verified before cut-over |

## 16. Out of scope

Multi-tenancy and a super-admin, electronic register integrations, real-time notifications, and any email-based flow.
