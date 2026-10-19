# Project Specifications: School Timetable Management and Optimization Platform

**Reproducible** reference document for the domain: formats, formulas, weights and rules precise enough to rebuild the
system. It was written from the first implementation (FastAPI backend `app/`, Node frontend `frontend/`); this
repository is the Django rebuild described in `architecture.md`. The domain rules (constraints, weights, calendar
resolution, permission matrix) apply unchanged; where the stack or layout differs (framework, routes, file paths,
frontend), `architecture.md` and the code win. Where a feature is planned but not implemented, this is stated
explicitly (§17).

Conventions: table, field, endpoint and constant names are the ones used in the code; dates are ISO `YYYY-MM-DD`; days
in the data are `LUNEDI…VENERDI` (uppercase, no accent) in the database and in the timetable APIs, `lunedi…venerdi`
(lowercase) in the availability grids; lesson hours are the slots `8..13` (08:00-14:00). The application UI is in
Italian: user-facing strings are quoted verbatim in Italian, with an English gloss in parentheses on first use.

---

## 1. Vision, scope and glossary

Web platform to generate, review and publish the **weekly school timetable** of a training institution
(Agenzia Formativa, upper secondary schools / IeFP vocational courses). A constraint solver (Google OR-Tools CP-SAT)
produces the timetable one week at a time starting from: the annual calendar, master data, teacher-class-subject
assignments, remaining hours budget and teacher availability. The administrator reviews the result, resolves
conflicts, approves it and exports it as a PDF/Excel board.

**v1 scope (implemented):** a single school (single-tenant, `scuola_id = "sch_1"`), login access (roles
ADMIN and SEGRETERIA, i.e. secretariat), file-driven guided setup, teacher availability, timetable
generation/editing/approval, PDF and Excel export, weight learning from the administrator's ratings, deploy on a VM
with Docker.

| Term | Meaning |
|---|---|
| Classe (class) | Group of students, name `"<roman numeral> <course>"`, e.g. `II ELETTRICISTI`, `I I.T.C.` |
| Gruppo (group) | Cohort by year: `PRIME` (first years), `SECONDE` (second), `TERZE` (third), `QUARTE` (fourth), `PRIMA4+2` (the class `I I.T.C.`, course "4+2") |
| Assegnazione (assignment) | Triple teacher + class + subject (one row of `assegnazione`) with its hours budget |
| Docente ASSUNTO (permanent teacher) | Always available 8-14 from Monday to Friday (not constrained by a grid) |
| Docente CONTRATTO (contract teacher) | Available only in the slots marked in the week's grid |
| Ora buca (gap hour, teacher) | Free hour of a teacher between two of their lessons on the same day |
| Stage / tirocinio (work placement) | The class/group is off-site: zero lessons, the timetable shows "STAGE" |
| Deroga / quick action (waiver) | Explicit relaxation, for a single regeneration, of a near-hard rule |
| Settimana (week) | Monday-Friday, identified by the Monday date (`week_start`) |

---

## 2. Architecture and stack

```
Browser ──HTTPS──> Caddy ──/api/*, /health, /docs, /openapi.json, /redoc──> backend (FastAPI :8000) ──> PostgreSQL
                      └────────── everything else ──────────────────────> frontend (Node :3000, SSR)
```

| Component | Technology (versions from `requirements.txt` / `package.json`) |
|---|---|
| Backend | Python 3.13, FastAPI ≥0.104, Uvicorn, SQLAlchemy 2.0 (not 2.1), Pydantic 2 + pydantic-settings, psycopg2 |
| Solver | `ortools` ≥ 9.12 (CP-SAT) |
| PDF | WeasyPrint ≥ 60 (requires Pango/Cairo; fonts: Liberation/DejaVu) |
| Excel | openpyxl ≥ 3.1 |
| Incoming PDF reading | pdfplumber ≥ 0.11 |
| LLM (copilot, currently disabled) | `anthropic` SDK, tool-use |
| Frontend | React 19, TanStack Start/Router/Query, Tailwind CSS 4, Vite, nitro `node-server` build |
| Database | PostgreSQL 15 (production); SQLite for development and tests |
| Reverse proxy | Caddy 2 (automatic Let's Encrypt HTTPS) |

Repository structure:

```
app/main.py            FastAPI app, CORS, create_all + admin.role column migration
app/config.py          Settings (env): DATABASE_URL, CORS_ORIGINS, SOLVER_TIMEOUT_SECONDS, ANTHROPIC_API_KEY, TEMP_DIR…
app/database.py        engine, SessionLocal, Base, get_db, ensure_admin_role_column()
app/models.py          SQLAlchemy models (§3)
app/schemas.py         Pydantic request/response models
app/security.py        password hashing, sessions, role guards (§4)
app/routes/            auth, setup, availability, schedule, chat, dati, soft_weights
app/services/          application logic (schedule, setup, availability, dati, feedback, soft_weights, chat)
app/repositories/      DB access (schedule: weekly context, stage cells; setup; availability)
app/domain/            pure logic: solver.py, parser.py, validator.py, soft_weights.py, nlp.py
scripts/               create_admin.py, import_documenti.py, rederive_monte_ore_and_pairings.py,
                       set_contratto_availability.py, infer_availability_from_calendars.py
frontend/src/routes/   index (login), menu, setup, disponibilita, orario, dati.*, account
frontend/src/lib/api.ts HTTP client with Bearer token
deploy/                docker compose, Caddyfile, update.sh, backup, create-admin.sh
tests/                 pytest (temporary SQLite)
Documenti/             real sample PDFs (annual calendar, sample timetables)
```

Configuration (`.env`, see `.env.example`): `DATABASE_URL`, `API_TITLE`, `API_VERSION`, `DEBUG`,
`SOLVER_TIMEOUT_SECONDS=60`, `ANTHROPIC_API_KEY`, `USE_S3`, `S3_BUCKET`, `TEMP_DIR=/tmp/calendariodocenti`,
`CORS_ORIGINS` (JSON list; default `["*"]`, auth is a Bearer header, not a cookie, hence CORS without credentials).

At startup `app/main.py` runs `Base.metadata.create_all` and `ensure_admin_role_column()`: `create_all` does not alter
existing tables, so it adds `admin.role` to databases that predate roles (`ALTER TABLE admin ADD COLUMN role VARCHAR(20)
NOT NULL DEFAULT 'ADMIN'`). There is no migration system (Alembic): every new column requires a similar migration
function.

---

## 3. Data model

All ids are UUID strings (`str(uuid4())`) except `scuola.id`, which in v1 is `"sch_1"`. `created_at` is
`DateTime server_default now()`. Enumerations are SQLAlchemy `Enum`s.

| Table | Columns (type, constraints) |
|---|---|
| `scuola` | `id`, `nome` (255, NN), `anno_formativo` (50, NN, e.g. `2026-2027`), `data_inizio_anno` (Date, NN), `data_fine_anno` (Date, NN) |
| `classe` | `id`, `scuola_id` FK, `nome` (50, NN), `n_studenti` (int, def 0), `gruppo` (50, nullable) |
| `docente` | `id`, `scuola_id` FK, `nome` (255, NN, format **"COGNOME NOME"**, i.e. surname first), `email` (nullable), `tipo` Enum(`ASSUNTO`,`CONTRATTO`) NN, `active` (bool, def true), `first_time_this_year` (bool, def true) |
| `materia` | `id`, `scuola_id` FK, `nome` (255), `tipo` Enum(`TEORIA`,`PRATICA`), `peso_cognitivo` Enum(`ALTO`,`MEDIO`,`BASSO`) |
| `assegnazione` | `id`, `scuola_id`, `docente_id` FK, `classe_id` FK, `materia_id` FK |
| `monte_ore_annuale` | `id`, `scuola_id`, `classe_id`, `materia_id`, `docente_id` (FK), `ore_totali` (int NN), `ore_erogate` (int, def 0). **Remaining = `ore_totali − ore_erogate`** (see §8) |
| `calendario_annuale` | `id`, `scuola_id`, `data` (Date NN), `gruppo` (50, null = row valid for the whole school), `ore_max_giornata` (int, def 6; values 4/5/6, 0 on a closure row, 8 = group stage with `gruppo`), `ora_inizio_min` (int 8-13, null), `flag_chiusura` (bool), `flag_stage_classe_id` (FK classe, null), `flag_stage_gruppo` (bool) |
| `disponibilita_settimanale` | `id`, `scuola_id`, `docente_id`, `settimana_inizio` (Date), `giorni_fasce` (JSON, §6), `created_at`, `updated_at` |
| `classe_accoppiata` | `id`, `scuola_id`, `classe_a_id`, `classe_b_id`, `materia_id` (FK), `docente_id` (FK, null), `note` (Text) |
| `orario_settimanale` | `id`, `scuola_id`, `settimana_inizio` (Date), `stato` Enum(`BOZZA`,`APPROVATO`) def BOZZA, `quality_score` (Float 0-100), `quality_level` Enum(`A`,`B`,`C`), `n_conflitti_soft` (int), `approved_at` |
| `slot_lezione` | `id`, `orario_settimanale_id` FK, `classe_id`, `docente_id`, `materia_id`, `giorno` Enum(LUNEDI…VENERDI), `ora_inizio` (8-13), `ora_fine` (9-14), `accoppiata` (bool), `classe_accoppiata_id` (FK classe, null) |
| `chat_message` | `id`, `scuola_id`, `orario_settimanale_id`, `ruolo` Enum(`ADMIN`,`AI`), `messaggio` (Text), `timestamp` |
| `preferenze_ai_memory` | `id`, `scuola_id`, `regola` (Text), `peso` (Float, def 1.0) |
| `conflitto_gestito` | `id`, `orario_settimanale_id` (index), `chiave` (300), `azione` (`APPROVATO` \| `LIBERA`). Emptied at every regeneration of the week |
| `orario_feedback` | `id`, `orario_settimanale_id` (index), `admin_id`, `voto` (1-5), `motivi` (JSON list of codes), `nota` (Text), snapshot `quality_score`/`quality_level`/`n_conflitti_soft`/`stato`. Append-only |
| `soft_weight_adjustment` | `id`, `kind` (60, index), `old_weight`, `new_weight`, `evidenza` (JSON), `admin_id`, `created_at`. Append-only: the active weight of a `kind` is the `new_weight` of the most recent row |
| `audit_log` | `id`, `scuola_id`, `orario_settimanale_id` (null), `azione` (e.g. `GENERATE`, `MODIFY_SLOT`, `SCHEDULE_APPROVED`, `QUICK_ACTION`), `admin_id`, `dettagli` (JSON), `timestamp` |
| `admin` | `id`, `scuola_id` FK, `email` (255, **unique**, index, stored lowercase), `password_hash`, `role` String(20) NN def `ADMIN` (`ADMIN` \| `SEGRETERIA`) |
| `admin_session` | `token` (PK, opaque), `admin_id` FK, `created_at`, `expires_at` |

Integrity notes:
- A paired class is stored as **one row per pair per teacher** (a group of 4 classes always together =
  several rows in pairs). `docente_id` is the teacher who actually gives the joint lesson: the other teachers of the two
  classes stay independent.
- There is no declared cascade delete: the `dati` services remove dependent rows by hand.

---

## 4. Authentication and roles

**Login:** `POST /api/auth/login {email, password}` → `{status:"ok", token, email, role}`. Error 401 «Email o password non
corretti» (wrong email or password). The token is opaque (`secrets.token_urlsafe(32)`), stored in `admin_session` with a **7-day** expiry; the
client sends it as `Authorization: Bearer <token>`. `POST /api/auth/logout` deletes the session (works even if
expired). `GET /api/auth/me` → `{email, role}`.

**Password:** PBKDF2-HMAC-SHA256, 260 000 iterations, random 16-byte salt; stored format
`"<salt_hex>$<digest_hex>"`; comparison with `hmac.compare_digest`. Minimum length 8 characters.

**Password change:** `POST /api/auth/change-password {current_password, new_password}` (requires login). 400 if the
current password is wrong, if the new one is shorter than 8 characters or equals the current one. After the change **all other
sessions of the same user are deleted**; the one in use stays valid.

**User creation:** there is no sign-up. Use `python -m scripts.create_admin <email> <password> [scuola_id]
[role]` (default `sch_1`, `ADMIN`); in production `deploy/create-admin.sh <email> "<password>" [ADMIN|SEGRETERIA]`.
If the user exists, its password and role are reset.

**Roles and permissions** (guards in `app/security.py`; a missing role counts as ADMIN):

| Area | ADMIN | SEGRETERIA |
|---|---|---|
| Login, `/auth/me`, password change | yes | yes |
| Reading the timetable (`GET /schedule/{w}` and derived), PDF/Excel export | yes | yes |
| Reading teachers (`GET /teachers`) and school data (`GET /classes, /subjects, /assignments, /class-pairings, /calendar`) | yes | yes |
| Teacher availability: read, save, copy from the previous week, status, list of weeks | yes | **yes** |
| Generate, regenerate, edit slots, assign slots, conflicts, quick action, approve, rate (`POST /schedule/**`) | yes | **no (403)** |
| Setup (`/setup/**`, file upload, `/calendar/add-date-manual`) | yes | no (403) |
| School data writes (POST/PUT/DELETE on classes, subjects, assignments, pairings, calendar) | yes | no (403) |
| Teacher CRUD (`POST/PUT/DELETE /teachers…`) | yes | no (403) |
| Copilot chat, solver weights (`/soft-weights`) | yes | no (403) |

Implementation: `require_admin` (ADMIN only, on the `setup`, `chat`, `soft_weights` routers and on teacher
writes), `require_admin_for_writes` (non-GET methods ADMIN only, on the `schedule` and `dati` routers), `get_current_admin`
(any logged-in user, on the `availability` router). 403 response: «Permesso negato…» (permission denied). No token/expired token → 401.

The frontend (§13) hides the functions forbidden to the secretariat, but permissions are always enforced server-side.

---

## 5. Initial setup (data loading)

Flow (page `/setup`): for each file type, upload → the file is read and validated → the admin sees the preview
and any warnings → corrects → `POST /setup/save`. The upload parks the already-parsed data in an in-memory cache of the
`SetupService` (a module-level instance); `/setup/validate` with `{file_ids:[…]}` validates cross-file consistency;
alternatively it directly accepts the CSV contents (`calendario, docenti, classi, materie, assegnazioni, accoppiamenti`).
The cache is per-process: a backend restart in the middle of setup loses it.

### 5.1 CSV files (header required, UTF-8)

| File | Upload endpoint | Columns |
|---|---|---|
| Calendar | `POST /calendar/upload` (CSV or PDF) | `data` (YYYY-MM-DD), `ore_max_giornata` (**4/5/6**; **0** only on a closure row; **8** = group stage, only with `gruppo` and without closure or `stage_classe_id`; anything else = error), `flag_chiusura` (true/1/yes), opt. `stage_classe_id`, `gruppo` |
| Teachers | `POST /teachers/upload` | `nome`, `email` (opt.), `tipo` (`ASSUNTO`/`CONTRATTO`) |
| Classes | `POST /classes/upload` | `nome`, `n_studenti` (opt.), `gruppo` (opt., e.g. `PRIME`) |
| Subjects | `POST /subjects/upload` | `nome`, `tipo` (`TEORIA`/`PRATICA`), `peso_cognitivo` (`ALTO`/`MEDIO`/`BASSO`) |
| Assignments | `POST /assignments/upload` | `docente_nome`, `classe_nome`, `materia_nome`, `ore_anno` |
| Pairings | `POST /class-pairings/upload` | `classe_a`, `classe_b`, `materia_nome`, `note` (opt.) |

### 5.2 Calendar from PDF («CALENDARIO AF»)

The parser (`CalendarParser.parse_calendario_pdf`, pdfplumber) reads the rows of the main table. Geometry
(x0 coordinates in PDF points, constant `CALENDARIO_PDF_GRUPPI_COLONNE`):

| Group | x0 from | to |
|---|---|---|
| PRIME | 140 | 195 |
| SECONDE | 195 | 250 |
| TERZE | 250 | 305 |
| QUARTE | 305 | 360 |
| PRIMA4+2 | 360 | 410 |

The date column is everything at x0 < 140; the area read is cropped at x ≤ 410 (the side activity tables
are ignored). A date row looks like «lunedì 14 settembre 2026» (Italian months). For **each date and each group** a
row `{data, gruppo, ore_max_giornata, flag_chiusura=false, stage_classe_id=null, flag_stage_gruppo}` is created:
- empty cell → `ore_max_giornata = 0`;
- **cell with value ≥ 8 (`STAGE_ORE_GIORNATA`) → `flag_stage_gruppo = true`**: the group is on work placement that day
  (in the agency's PDF the footnotes give the ranges, e.g. «tirocinio classi IV dal 5/10 al 22/12», i.e. "year-4 classes on placement from 5/10 to 22/12");
- working days (Mon-Fri) between the first and last date read but missing from the PDF become rows
  `gruppo=null, ore_max_giornata=0, flag_chiusura=true` (closures/holidays).
The CSV (§5.1) also expresses these values: 0 on a closure row and 8 with `gruppo` (there is no `flag_stage_gruppo`
column: `flag_stage_gruppo = true` is derived from the 8 hours, one row per group and date); the hours rule is
the same for CSV, PDF/AI, manual date (which has no group: no 8) and cell correction. The calendar form in the data pages
(and the range page) also saves 0 hours on a closure and 8 hours on a group stage, derived from the «Chiusura» (closure)
and «Stage di gruppo» (group stage) checkboxes, with the same rule.
If no row is recognized the parser raises an error («serve un parser diverso per un altro template», i.e. "a different parser is needed for another template").
A different template requires updating the coordinates.

Saving (`bulk_create_calendar`) writes `gruppo`, `ore_max_giornata`, `flag_chiusura`, `flag_stage_gruppo` and
`flag_stage_classe_id` (it also accepts the parser's `stage_classe_id` key).

### 5.3 Other parsers

- `SchoolRosterParser.parse_orario_settimanale_pdf`: reads a published timetable («Orario scolastico dal X al Y…», i.e. "School timetable from X to Y…";
  rows = hours, columns = classes, cell = teacher surname) and returns `classi`, `slots {classe_nome, giorno,
  ora_inizio, docente_cognome}` and `week_label`. It is used to derive teacher-class associations and observed
  availability (script §6.3). The group is inferred from the class name: roman numeral → `PRIME/SECONDE/TERZE/QUARTE`; `I.T.C.` → `PRIMA4+2`.
- `parse_elenco_docenti_pdf`: teacher list from PDF.

### 5.4 Validation (`DataValidator.validate_all_data`)

**Errors** (block `can_proceed`): assignment with a nonexistent teacher/class/subject; pairing with a nonexistent class or
subject, or without an assignment for that class+subject on both classes; `stage_classe_id`
that is not a known class.
**Warnings** (non-blocking, correctable): teacher without assignments; class without assignments; unused subject.
Real-time corrections: `POST /setup/validate-field {file_type, entity, field, new_value}` and
`POST /setup/apply-correction {file_id, correction}`; missing date: `POST /calendar/add-date-manual {file_id, date,
ore_max_giornata (4/5/6), flag_chiusura, flag_stage_classe_id}`.

### 5.5 Saving

`POST /setup/save {file_ids, corrections_applied, school_name, school_year, data_inizio?, data_fine?}`. If the dates
are not provided, the calendar's min/max are used. It creates `scuola` (fixed id `sch_1`), classes, teachers, subjects, calendar,
assignments (with the related `monte_ore_annuale` row) and pairings in a single transaction; on error, rollback.
Response: `{status:"setup_complete", school_id, next_page}`.

After setup everything can be corrected from the «Dati scuola» (school data) pages (§13), which offer full CRUD.

---

## 6. Teacher availability

**`giorni_fasce` format** (required, validated: all 5 days, each day a non-empty list of slots with
`ora_inizio`, `ora_fine`, `disponibile`):

```json
{"lunedi": [{"ora_inizio": "08:00", "ora_fine": "09:00", "disponibile": true}, …6 slots 08-14…], "martedi": [...], …}
```

**Reading (`GET /availability/{week}/{teacher_id}`)**, in order: the week's record; if none exists and the teacher is in their
first year → default grid (everything available 8-14, `is_default=true`); otherwise inherit the most recent
previous week; otherwise default. `POST /availability/{week}/{teacher_id} {giorni_fasce}` saves;
`POST /availability/{week}/copy-from/{prev_week}` copies the grids skipping teachers already filled in;
`GET /availability/{week}/status` → `{week_complete, total_teachers, teachers_with_availability, teachers_missing,
ready_to_generate, completion_percentage}`; `GET /availability/weeks` → `[{start, end (=Friday), week_num}]` for each
Monday of the training year starting from the first Monday ≥ `data_inizio_anno`.

**Use in the solver** (`ScheduleRepository.get_week_context`): for each teacher without a record for the week, the
same inheritance applies (previous week, then default), read-only, persisting nothing. **ASSUNTO** teachers are always
available 8-14 (the grid does not constrain them); for **CONTRATTO** teachers the grid applies (§9, constraint H9). A contract teacher with
no recorded availability is treated as unavailable for the whole week.

### 6.3 Data initialization scripts (one-off, not part of the app)

- `scripts/import_documenti.py`: builds school, calendar, classes, teachers and associations by reading the PDFs in `Documenti/`.
- `scripts/rederive_monte_ore_and_pairings.py`: recomputes `ore_totali` (= observed weekly hours × remaining weeks)
  and the per-teacher pairings from the sample timetable, **resetting `ore_erogate` to zero**. It is only for initialization:
  since approval deducts hours (§8) it **must not be run again**, otherwise the hours already delivered and those to be recovered are lost.
- `scripts/set_contratto_availability.py`, `scripts/infer_availability_from_calendars.py`: derive the contract teachers'
  availability from the sample timetables (days = hours/6 rounded, chosen with a fixed seed; or union of the hours observed in two weeks).

---

## 7. Calendar: how a class is resolved on a given day

For each class and day the solver (and the stage cells) use this precedence (`_ore_max_for_classe_giorno_raw`):

1. row with `flag_stage_classe_id == class` → **0 hours** (stage);
2. row with `gruppo == class group` and `flag_stage_gruppo` → **0 hours** (group stage);
3. row with `gruppo == class group` → `0` if `flag_chiusura`, otherwise `ore_max_giornata`;
4. row with `gruppo = NULL` (whole school) → same;
5. no data → **6**.

`ore_max_giornata` is then capped at **6** (the variables only cover 8-13). `ora_inizio_min` (8-13) forbids lessons
before that hour for that class/group (staggered entry), with the same group → school precedence.
A class's stage days appear in the stage cells (`stage_cells`, §10) as «STAGE», without a teacher.

---

## 8. Hours budget and weekly target

- Remaining hours of an assignment: `ore_residue = ore_totali − delivered hours` (0 if the `monte_ore_annuale` row is missing). Delivered hours
  are not a column: they are the sum of the assignment's `LedgerEntry.hours` (`delivered_hours_map`), so the remaining value can
  be negative. `ore_totali` is the hours budget to be taught from the start of the managed period.
- **Deducting hours (admin decision 2026-10-05):** on **approval** of the week (`POST /schedule/{w}/approve`)
  the system writes, in the same transaction on the locked timetable row, **one `DELIVERED` entry of 1 hour for each lesson** for the
  assignment (teacher + class + subject) of its class and of the paired one (`paired_class_id`): in a paired lesson
  both assignments are deducted. Then the state becomes `APPROVATO`, `approved_at` is set, `version` is
  incremented and `SCHEDULE_APPROVED` is written to the audit log. Hours not taught (absences, skipped lessons) are not
  deducted: they stay in the remainder and later weeks recover them, because **each week's target is based on the
  remainder**, not on the total. The operation is idempotent: re-approving an already approved week writes nothing
  (`ore_scalate: 0`). Lessons with no matching assignment write nothing and are counted in `senza_monte_ore`.
- **Undoing an approval** (`POST /orario/{w}/annulla-approvazione/`, ADMIN only): requires a reason (trimmed,
  at most 300 characters) and an `APPROVATO` week (otherwise 409). In the same transaction, for each assignment
  a `REVERSAL` entry is written equal to minus the net of `DELIVERED` + `REVERSAL` for that timetable (zero nets are skipped),
  with reason and user; the week returns to `BOZZA`, `approved_at` is cleared, `version` incremented and
  `APPROVAL_UNDONE` is written to the audit log. Entries are never edited or deleted and `ADJUSTMENT` entries stay intact. After
  the undo the week can be edited, regenerated and re-approved.
- **Manual correction** (ADMIN only): the «Assegnazioni» (assignments) page has a form that takes the new total of delivered hours and a
  reason, and writes an `ADJUSTMENT` entry with the difference (reason and user). It rejects a negative total; if the total does not
  change it writes nothing; the audit log receives `HOURS_ADJUSTED`. Delivered hours cannot be edited directly.
- **Weekly target** per assignment: `settimane_rimaste = max(1, (data_fine_anno − week_start).days // 7 − settimane_senza_lezione)`, where
  `settimane_senza_lezione` are the future weeks in which the class, on all five days Mon-Fri, is on stage (class
  or group) or has a closure row (of the school or of its group); a week is counted only once
  even if it mixes stage and closure; a week with at least one teaching day (even just one, even a day without a row) is normal,
  `ore_target = max(1, ore_residue // settimane_rimaste)`. No target if `ore_residue ≤ 0` or if the class has no teaching
  hours in the whole week (e.g. the whole week on stage). Waiver `reduce_contract_hours` (contract teachers only):
  `ore_target = max(1, min(target, max_hours))`, or `max(1, target // 2)` if `max_hours` is not given.
- Hard limit: an **ASSUNTO** teacher never exceeds `ore_residue` per assignment (H8); a **CONTRATTO** teacher may
  exceed it (admin, 2026-10): they are limited only by availability.

---

## 9. Optimization engine (CP-SAT)

### 9.1 Model

Boolean variables `x[(assegnazione_id, day 0-4, hour 0-5)]` (hour 0..5 = 8..13). A lesson = a variable set to 1.
Objective function: **minimize** `Σ weight_i · violation_i` over all penalties (`SoftPenalty`). Maximum time
`SOLVER_TIMEOUT_SECONDS` (60 s). Statuses: `OPTIMAL`, `FEASIBLE`, `INFEASIBLE`, `UNKNOWN`.

Input context (`ScheduleContext`, built by `get_week_context`): assignments with remainder, availability map,
calendar of the 5 days (missing → 6 hours), paired classes `(classe_a, classe_b, materia, docente)`, teacher→type
map, class→group map, `anno_fine`.

### 9.2 Hard constraints (always respected)

| Id | Rule |
|---|---|
| H1 | A teacher is not in two classes in the same hour (exception: paired lesson) |
| H1b | A class has at most one lesson per hour |
| H2 | **Exact capacity:** lesson hours of a class in a day = the day's maximum hours (§7). No free hour: if impossible, the week is INFEASIBLE |
| H2b | A class's lesson hours in a day form **a single contiguous block** (no gap hour for the class). On a short day the free hour(s) are at the start or at the end |
| H2c | No lessons before the class's `ora_inizio_min` |
| H3 | **Pairings:** for each pair, the indicated teacher has the lesson on the same day/hour in both classes and the hours budget is deducted **only once** |
| H5 | Classes on stage: zero lessons (§7) |
| H6 | ASSUNTO teacher: weekly hours per assignment ≤ remainder (see §8) |
| H7/H8 | Exclusions and caps requested by the chat for that run only (day excluded for a teacher; maximum weekly hours) |

### 9.3 «Near-hard» constraints (relaxable only at weight `RELAX_WEIGHT` = 10_000_000_000, always reported)

Each has an `override` variable with weight **`RELAX_WEIGHT = 10_000_000_000`**: the solver violates them only if no
alternative exists and every violation appears among the conflicts with the corresponding waiver (no silent violations).

| Id | Rule | Waiver that authorizes it |
|---|---|---|
| H4 | **Contract teacher availability**: no lesson where `disponibile=false`. In generation it is **hard** (never forced); the penalty (`RELAX_WEIGHT`, 10_000_000_000) exists only for the admin's manual edits (*forced* mode, §10) | `override_availability` |
| H9 | Friday: a class's block of lessons **starts at 8:00** (admin decision 2026-09-23); not applied if the class has no lessons or has an `ora_inizio_min` | `authorize_friday_late_start` |
| H10 | **Practical**: the hours of a PRATICA subject form **a block of at least `min(3, capacity)` hours** in the day, or none (admin decision 2026-09-23) | `authorize_short_pratica_block` |
| H11 | A teacher teaches at most **4 hours** (`MAX_HOURS_PER_CLASSE_DAY`) with the same class on the same day; it applies to teachers with ≥ 2 classes; paired lessons count once | `authorize_single_classe_day` |

### 9.4 Preferences (soft) and weights

| Id | Rule | Default weight | Notes |
|---|---|---|---|
| S1 `classe_late_start` | A class starts at 8:00 | **500** | highest weight among the preferences; inactive if the day is full |
| S2 `teoria_consecutive` | Max 2 consecutive hours of theory with the same teacher in the same class | **50** | waiver `force_3_hours_theory` raises the cap (3 or `max_hours`) |
| S3 `ore_target_deviation` | Weekly hours of the assignment = target (§8) | **75 + 10_000_000** per hour of deviation | 10_000_000 = `EXACT_TARGET_WEIGHT` (admin decision 2026-10: "exact" target, yields only to fill a classroom). For contract teachers see 9.5 |
| S4 `contractor_gap` | No **true gap hour** for a contract teacher (free hour with a lesson before and after on the same day) | **750** per gap hour | CONTRATTO only: gaps therefore fall on permanent teachers (no penalty). For contract teachers see 9.5. Waiver `authorize_early_exit` switches it off |
| S5 `single_classe_day` | A teacher with ≥2 classes does not spend a whole day (≥3 hours) with a single class | **50** | waiver `authorize_single_classe_day` |

Adjustable weights (`DEFAULT_SOFT_WEIGHTS`, cap `MAX_SOFT_WEIGHT = 1_000`): `classe_late_start 500`,
`teoria_consecutive 50`, `ore_target_deviation 75`, `contractor_gap 750`, `single_classe_day 50`. Fixed weights:
`RELAX_WEIGHT 10_000_000_000` (near-hard constraints), **1_000_000_000_000** (`FIXED_PENALTY_WEIGHT`) per uncovered
hour (`classe_unfilled`), hour over capacity (`classe_over_capacity`) and exceeded hours budget in *forced* mode
(`monte_ore_exceeded`): an empty class is never traded for a preference. Priority order: **uncovered hours (1e12) > near-hard
constraints (1e10) > exact target (1e7+) > preferences (≤ 1_000)**, a decade apart.

Sum in the worst case (one school: 40 classes, 5 days, ≤ 30 hours per class per week, ≤ 100 teachers): the
preferences weigh at most `1_000 · 1.5 = 1_500` per violation, so ≤ 2_000 violations sum to ≤ 3.0e6, less than the
cheapest hour of deviation from the target (`0.5 · 1e7 = 5.0e6`); ≤ 200 hours of deviation · ~1.5e7 = 3.0e9 < `RELAX_WEIGHT`;
≤ 50 waivers · 1e10 = 5.0e11 < `FIXED_PENALTY_WEIGHT`; the maximum objective (≈ 1_200 uncovered hours · 1e12 = 1.2e15)
stays below 2^53 (exact conversion in CP-SAT). A violation of a higher level is therefore never traded for a
realistic number of violations of the lower levels. The quality score (9.6) does not depend on the weights.

### 9.5 Contract teachers: scarcity of availability

The weight of S3 and S4 for a contract teacher is multiplied by `m = 1.5 − (available_hours / 30)` (from 0.5 to 1.5):
whoever has very little availability gets higher penalties (their target is respected more and their gaps are avoided,
they are "placed first"); whoever is very available has more flexibility. No grid recorded → `m = 1.5`;
waiver `override_availability` active for that teacher → `m = 1.0`; non-contract teachers → `m = 1.0`. The resulting weight is
`max(1, round(weight · m))`. Values: gap 375…1125, deviation from target ≈ 5_000_038…15_000_112 (computed from 10_000_075, if the S3 weight is not tweaked).

### 9.6 Quality score

`score = (penalties at value 0 / number of penalties) × 100`; level **A** if > 95, **B** if > 80, otherwise **C**.
If the solver finds no solution: score 0, level C. `n_soft_conflicts = number of violated penalties`.

### 9.7 Fallback and waivers

`_solve_with_fallback`: (1) hard model (H2 equality); if `INFEASIBLE` (2) **best-effort mode**: uncovered hours
become penalties at `FIXED_PENALTY_WEIGHT` = 1_000_000_000_000 (`classe_unfilled`) and the best possible timetable is returned with the message «Nessun
orario completo possibile…» ("No complete timetable possible…"); (3) for a manually forced timetable (`solve_fixed` with a pinned assignment) a further
**forced** mode in which capacity, double booking, hours budget, start and stage become reported conflicts instead of errors.

**Quick action / waiver** (`POST /schedule/{w}/apply-quick-action {action_type, class_id?, teacher_id?, giorno?,
max_hours?}`), valid for **a single regeneration** (not persisted), with optional class/teacher/day scope:

| `action_type` | Effect |
|---|---|
| `force_3_hours_theory` | Cap of consecutive theory hours set to 3 (or `max_hours`) |
| `reduce_contract_hours` | Contract teacher's target reduced (§8) |
| `authorize_early_exit` | No penalty on the contract teacher's gaps |
| `override_availability` | Ignores the teacher's recorded availability |
| `authorize_single_classe_day` | Disables H11/S5 for teacher/class/day |
| `authorize_friday_late_start` | Disables H9 |
| `authorize_short_pratica_block` | Disables H10 |

### 9.8 Conflicts

`get_conflicts()` lists the violated penalties of the solution, deduplicated by `(kind, class, teacher, day, hour)`:
`{conflict_id, chiave, description, suggested_action {action_type, label}, classe_id, docente_id, giorno, ore?, ore_slot?}`.
`chiave = "<kind>|<classe_id>|<docente_id>|<DAY>[|<hour>]"` is stable across recomputations (unlike `conflict_id`).
For `ore_target_deviation` the description reports assigned hours/target/deviation and how the target was derived; the
«Riduci ore» (reduce hours) waiver is suggested only for a contract teacher **above** target.

---

## 10. Lifecycle of a week's timetable

States: absent → `BOZZA` (draft) → `APPROVATO` (approved, locked).

| Operation | Endpoint | Behavior |
|---|---|---|
| Generate | `POST /schedule/generate {week_start, include_preferences=true}` | Rejects if the week is APPROVED. Error if there are no assignments. Solves (§9.7). Response `status`: `generated` (with `schedule_id, quality_score, quality_level, n_soft_conflicts, slots, conflicts, stage_cells, message?`), `infeasible` (with `conflicting_constraints`, `suggested_deroghe`), `timeout`, `error`. The weights used are the active ones (default + learned adjustments, §11) |
| Regenerate | `POST /schedule/{w}/regenerate` | Replaces the week's slots (same `schedule_id`), empties `conflitto_gestito` |
| Read | `GET /schedule/{w}` | Slots, quality, conflicts (recomputed at every read and filtered by the admin's decisions), `stato`, `stage_cells`; 404 if absent |
| Quality | `GET /schedule/{w}/quality-score` | Score, level, number of conflicts |
| Move a lesson | `POST …/modify-slot {slot_id, changes{docente_id?, giorno?, ora_inizio?}}` | Rejects if APPROVED; checks `ora_inizio` 8-13 and valid day; re-evaluates with `solve_fixed` (pinned variables) and recomputes quality/conflicts |
| Assign a free hour | `GET …/assignable/{classe_id}` lists teacher/subject pairs; `POST …/assign-slot {classe_id, giorno, ora_inizio, docente_id, materia_id}` | Forcing evaluated as a manual edit (violations become conflicts); rejected only if it exceeds the class's daily maximum hours. A teacher in a paired lesson is assigned to all the classes of the group |
| Conflicts | `POST …/conflicts/approve {chiavi}` (the conflict is no longer reported); `POST …/conflicts/reject {classe_id, giorno, ora_inizio}` (removes the lesson and leaves the hour free, `azione=LIBERA`) | Decisions in `conflitto_gestito` |
| Waiver | `POST …/apply-quick-action` | §9.7 |
| Approve | `POST …/approve` | `stato=APPROVATO`, `approved_at`, **deducts the lessons' hours from the hours budget (§8)**, entry in `audit_log` (`SCHEDULE_APPROVED` with `ore_scalate`, `assegnazioni_aggiornate`, `senza_monte_ore`); response `{status, schedule_id, stato, ore_scalate, assegnazioni_aggiornate, senza_monte_ore}`; from then on no edit/regeneration; idempotent |
| Export | §12 | PDF and Excel |
| Rate | `GET/POST …/feedback` | §11 |

Every returned slot has: `slot_id, classe_id, classe_nome, docente_id, docente_nome, materia_id, materia_nome,
materia_tipo, giorno, ora_inizio, ora_fine, accoppiata, classe_accoppiata_id, conflitto, conflitto_chiavi, indisponibile`.
`stage_cells`: `[{classe_id, classe_nome, giorno}]` for each class on stage (class or group) across the 5 days;
the `classe_id` in a stage record may also be the class **name** (it is resolved, case-insensitively, by
name if it does not match an id).

Manual edits and waivers write to `audit_log`.

---

## 11. Ratings and weight learning

- `POST /schedule/{w}/feedback {voto 1-5, motivi[], nota ≤ 2000}`: saves an append-only row with the quality snapshot.
  `motivi` codes (`MOTIVI`): `classe_late_start`, `friday_late_start`, `teoria_consecutive`, `pratica_block`,
  `contractor_gap`, `single_classe_day`, `ore_target_deviation`, `classe_unfilled`, `altro` (other). Each is linked to a
  solver penalty (`MOTIVO_CONSTRAINTS`); only five have an adjustable weight.
- **Proposals** (`GET /soft-weights`): for a reason with ≥ **2** low ratings (score ≤ 3) it proposes to **raise** the
  weight: step `+10% × severity` (1 star = 3, 2 = 2, 3 = 1), at most **+50%** per step and never above `MAX_SOFT_WEIGHT` = 1_000. Each
  rating counts once (after an adjustment or a reset only the later ones count).
- `POST /soft-weights/apply {kinds?}` accepts the proposals (all or some); `POST /soft-weights/reset {kind?}` restores the
  default of one weight or of all. Each change is a row in `soft_weight_adjustment` (history). Nothing changes on its own.
  The active weights apply from the next generations.

---

## 12. Export

`GET /schedule/{w}/export-pdf` → `{status:"generated", filename, pdf_url:"/api/schedule/{w}/export-pdf/file"}`;
`…/export-pdf/file` responds `application/pdf` (inline, `orario_{w}.pdf`). Similarly for `export-excel`
(`xlsx_url`, `…/export-excel/file`, `.xlsx`). The files require the Bearer token (the frontend downloads them with `fetch` and
opens/saves them as blobs; a direct `window.open` would give 401). A timetable can be exported in any state.

**Layout (identical for PDF and Excel), modeled on `Documenti/es_di_calendario.pdf`:**

- **PDF:** a single **landscape A4** page, 8 mm margins, font Calibri/Carlito/Helvetica.
  - Light-blue band `#95b3d7`, black bold 14 pt centered text: row 1 «Agenzia Formativa don Angelo Tedoldi», row 2
    «Orario scolastico dal 5 al 9 Ottobre» ("School timetable from 5 to 9 October"; Italian month names with initial capital; if the week spans two months:
    «dal 28 Settembre al 2 Ottobre»).
  - Table header on red `#c00000` background, white bold text: `DATA | Orario | <classes>`.
  - **Class order:** by course `OP. INFORM.` → `ELETTRICISTI` → `ESTETISTE` → `PAN. E PAST` → `I.T.C.`, within the course
    by year I, II, III, IV; unrecognized names last, in alphabetical order.
  - Five blocks (Monday…Friday), each with the `DATA` column merged vertically and the day label in
    lowercase rotated by 90°, white italic on orange `#e46c0a` (days 1,3,5) or blue `#0070c0` (days 2,4); rows
    `8-9, 9-10, … 13-14`; between one day and the next a grey strip `#d9d9d9`. Trailing rows with no lesson at all on
    that day (e.g. a short Friday) are omitted.
  - Cell: **only the teacher's surname** (first word of `docente.nome`), bold 5.8 pt centered, black 0.5 pt border;
    background by (course, year): OP. INFORM. I `#ddd9c4`, II `#f2dcdb`, III `#92d050`; ELETTRICISTI I `#ddd9c4`, II `#f2dcdb`,
    III `#ff99cc`; ESTETISTE I `#c5d9f1`, II `#fde9d9`, III `#ff99cc`; PAN. E PAST I `#c5d9f1`, II `#fde9d9`,
    III `#92d050`; I.T.C. I `#ffff99`; year IV and unrecognized: white.
  - Cell with no lesson: grey `#d9d9d9`, empty. **Stage cell: text «STAGE», yellow background `#ffff00`, no teacher.**
  - Paired lessons are not merged: the surname is repeated in each class.
- **Excel:** a single sheet «Orario» (timetable) with the same structure (title merged across the full width, red header,
  merged day column with rotated text, identical colors, columns 15 characters wide, `freeze` panes on the header and first
  two columns, landscape A4 page fitted to one page).

---

## 13. Frontend

React SPA (TanStack Router, file-based routing in `frontend/src/routes/`) served via SSR by Node; calls to `/api/*`
with `fetch` and Bearer token in `sessionStorage` (`authToken`, `authRole`). A 401 (except on login) clears the token
and returns to login. Pages:

| Route | Content |
|---|---|
| `/` | Login (email, password); saves token and role |
| `/menu` | Overview with the cards (Setup, Disponibilità, Orario, Dati scuola) |
| `/setup` | File upload, preview, warnings with real-time correction, adding dates, saving |
| `/disponibilita` | Week selector, hours × days grid per teacher, «copia dalla settimana precedente» (copy from the previous week), completion status |
| `/orario` | Week selector; score and level; classes × hours table per day (Monday-Friday tabs), theory/practical colors, highlighted conflicts, click on a teacher to highlight their presences, **Genera nuovo orario / Genera da zero / Approva settimana** (Generate new timetable / Generate from scratch / Approve week), quick actions (waivers), conflicts/edit panel on the right, star rating with reasons and the «Apprendimento dai voti» (learning from ratings) panel, **Esporta PDF / Esporta Excel** (Export PDF / Export Excel) |
| `/dati/*` | CRUD of calendar, teachers, classes, assignments, pairings (the «Singola classe in stage» (single class on stage) field is a menu of classes) |
| `/account` | Password change (current, new ≥ 8 characters, confirmation) |

**SEGRETERIA** interface (`isSegreteria()`): the side menu keeps Panoramica (overview), Disponibilità, Orario; the
Orario screen is read-only (no Generate/Approve/quick actions/side panel, cells not clickable);
user label «Segreteria / Sola lettura» (secretariat / read-only). At the bottom of the menu: «Cambia password» (change password) and «Esci» (log out).

Stage column in the timetable table: yellow cell (`bg-yellow-300`, border `border-yellow-500`) with «STAGE».

---

## 14. AI copilot (chat): feature present but disabled

`POST /chat/send` (ADMIN only, SSE response: `thinking`, `clarification`, `suggestion`, `ready`, `error`) and
`GET /chat/history/{w}`. Parsing uses the Claude API with tool-use: one function per intent: `move_lesson`,
`reduce_workload`, `exclude_day`, `set_soft_constraint`; it asks for clarification if parameters are missing. The intents are
actually applied (in-place regeneration with additional constraints H7/H8, lesson move, cap on consecutive
theory hours). In the frontend it is hidden by the flag `COPILOT_ENABLED = false` in `orario.tsx`. It requires
`ANTHROPIC_API_KEY`.

---

## 15. Deploy and operations

- **Stack:** `docker-compose.yml` with `postgres:15-alpine` (data volume, healthcheck), `backend` (Python 3.13 Dockerfile
  with Pango/Cairo/fonts), `frontend` (Node 22, multi-stage build, `node .output/server/index.mjs`, port 3000) and `caddy:2`
  (80/443, `DOMAIN` from env). Only Caddy exposes ports. The `Caddyfile` routes `/api/*`, `/health`, `/docs`, `/openapi.json`,
  `/redoc` to the backend `:8000` and the rest to the frontend.
- **Configuration:** `.env.production` (not versioned; from `deploy/generate-secrets.sh`): `DOMAIN`, `POSTGRES_USER`,
  `POSTGRES_PASSWORD`, `POSTGRES_DB`, `DATABASE_URL`, `ANTHROPIC_API_KEY`, etc.
- **First start:** `deploy/up.sh`; then create the users with `deploy/create-admin.sh` (§4); on a VM with 1 GB of RAM
  `deploy/setup-swap.sh` (4 GB of swap).
- **Update:** on the VM `deploy/update.sh` (`git pull --ff-only`, `docker compose up -d --build`, `image prune`).
  On a 1 GB VM the build is slow (tens of minutes, `pip install` included); alternatively build the images
  elsewhere. At backend startup the `admin.role` column is added if missing.
- **Backup:** `deploy/backup-db.sh` (local dump, nightly cron), `deploy/backup-offsite.sh` (Object Storage, weekly,
  to be configured), `deploy/restore-db.sh`, `deploy/dump-local-db.sh`.
- **Current production:** Oracle Always Free VM, `https://130-110-14-231.sslip.io`. `sslip.io` resolves a name only if it
  contains the IP; for a clean name you need your own domain (A record to the IP) and update `DOMAIN`.
- **Local development:** `python -m venv .venv && .venv/bin/pip install -r requirements.txt`;
  `DATABASE_URL=sqlite:///./dev.db .venv/bin/python -m uvicorn app.main:app --port 8000`; frontend
  `cd frontend && VITE_API_BASE_URL=http://localhost:8000 npm run dev` (port 8080). With Anaconda the `ortools` import
  may segfault: use a system venv.

---

## 16. Tests and acceptance criteria

`pytest` (`tests/`, temporary SQLite, `tests/conftest.py` excludes authentication from the API client and recreates the schema at
every test). Coverage: solver (hard and soft constraints, pairings, closures, stage, contract teachers: hours budget can be exceeded,
availability never violated, scarcity multiplier), parser (CSV and synthetic PDF), validation, `/api/*` endpoints
end-to-end, roles (`tests/test_roles.py`), password change (`tests/test_auth_password.py`), export, ratings and
weights. **Known state:** 6 tests already fail on a clean checkout (pairings with a shared teacher, practical blocks,
detailed conflicts, quick action `override_availability`, editing only one side of a pair); they are not
regressions of the features described here.

Acceptance criteria for a generated timetable (verifiable): (1) every class has exactly the day's hours (or the
`classe_unfilled` conflict says so); (2) no teacher in two classes in the same hour (except pairings); (3) no
lesson for classes on stage, which show «STAGE» in yellow in the export; (4) no lesson of a contract teacher outside
availability; (5) the assignment's hours are close to the target and in any case not above the remainder for permanent teachers;
(6) the PDF has the same structure as the model in §12.

---

## 17. Known limits and not implemented

- **Multi-tenant and Super-Admin**: not implemented (single-tenant, `scuola_id` fixed to `sch_1`).
- **Teacher portal** (teacher login, self-service availability entry, «confirm last week's timetable»,
  weekly deadline with automatic inheritance on the teacher side): not implemented; availability is entered
  by the administrator or the secretariat. Inheritance from the previous week already exists on read (§6).
- **Real-time sick-leave substitutions**: not implemented.
- **Permanent preference memory** (`preferenze_ai_memory`): table present but not used by the solver.
- **Undoing an approval**: applies to the whole week; there is no partial undo (for single lessons). To fix single assignments use the manual correction (§8).
- **Teacher names**: the surname on the board is the first word of «COGNOME NOME»; compound surnames are truncated.
- **Setup cache** in process memory (lost on restart).
- **Calendar PDF template**: the parser depends on fixed coordinates (§5.2).
- **Real subjects**: the imported data often use a placeholder subject («Materia da definire», "Subject to be defined"); the theory/practical constraints
  make full sense only with the real subjects configured.
- **Electronic register integration** (Spaggiari, ClasseViva, Axios): v2 roadmap.
- Generation on a 1 GB RAM VM is slow under load; the solver has a 60 s timeout.
