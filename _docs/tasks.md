# Tasks

Build order for the rebuild. `specs.md` is the scope and the source of domain rules, `architecture.md` the design.
Section references (§) point to `specs.md` unless stated otherwise. Tasks are ordered so that each one only needs the
ones before it, but each description says what it assumes, so it can be handed over on its own.

## 1. Empty project with a passing test
Goal: A Django project that installs, runs, and passes one test.
Description: Create the project with `uv`, with the `config/` package, an empty `tests/` directory and pinned
dependencies in `pyproject.toml`. Add one test that requests the home URL and gets a 200 response, and make
`uv run manage.py runserver`, `migrate` and `uv run pytest` all work on a clean checkout.

## 2. Lint and format setup
Goal: `ruff check` and `ruff format --check` pass and are documented.
Description: Add ruff configuration to `pyproject.toml` with sensible rules for a Django project, and fix whatever the
empty project reports. Document the two commands in the README so they can be run before every commit.

## 3. Environment-driven settings
Goal: Every setting comes from the environment, with a strict production config and a separate test config.
Description: Move secret key, debug flag, allowed hosts, database URL and solver timeout to environment variables read
in `config/settings.py`, failing loudly when a required one is missing. Add `.env.example` listing each variable and
`config/settings_test.py` supplying values for pytest.

## 4. Users, roles and login
Goal: Admins and secretariat staff can log in, log out and change their password.
Description: Add a user model with a `role` field (`ADMIN`, `SEGRETERIA`), login and logout views, and a change-password
page requiring the current password (minimum 8 characters, must differ from the current one). Add a management command
to create a user with a role, and document `manage.py changepassword` as the only password-reset path, since there is no
email.

## 5. Permission predicates
Goal: One module holds every authorization rule as a predicate function.
Description: Create `permissions.py` with functions taking a user and a domain object (for example `can_generate`,
`can_edit_school_data`, `can_edit_availability`) implementing the matrix in §4. Add a decorator or mixin that views use
to call predicates, with tests per role, and no inline role checks anywhere else.

## 6. Base layout and navigation
Goal: Every page shares one layout whose menu depends on the user's role.
Description: Build the base template with the vendored HTMX and Alpine files, a responsive navigation, flash messages and
a footer with change-password and logout links. Show Overview, Availability and Timetable to both roles, and the setup
and school-data entries to admins only, using the predicates from task 5.

## 7. School, class, teacher and subject models
Goal: The core master-data tables exist with migrations.
Description: Create models for school, class (name, student count, group), teacher (name in "SURNAME NAME" form, type
`ASSUNTO`/`CONTRATTO`, active, first-time flag) and subject (kind `TEORIA`/`PRATICA`, cognitive weight) as in §3. Add
model tests for constraints and string representations, and register them in the Django admin for debugging.

## 8. Assignments, hours and class pairings models
Goal: Teacher-class-subject assignments, their yearly hours, and paired classes can be stored.
Description: Add the assignment model with its total yearly hours, and the class-pairing model (two classes, subject,
optional teacher, note) as in §3, including the rule that one pairing is one row per pair per teacher. Add a helper
returning the hours target for a week from the remaining hours as in §8, with unit tests for the formula and the
contractor reduction.

## 9. Calendar model and day resolution
Goal: The annual calendar is stored, and the hours a class has on a given day are computed by one function.
Description: Add the calendar model (date, optional group, max hours, closure flag, start-hour limit, class or group
stage flags) and a pure function implementing the precedence in §7: class stage, group stage, group row, whole-school
row, default of 6 hours. Cover each precedence rule and the cap at 6 hours with tests.

## 10. Teachers screens
Goal: Admins can list, add, edit and delete teachers from the browser.
Description: Build the list, create, edit and delete views with forms and HTMX partial updates, restricted to admins for
writes and readable by the secretariat. Deleting a teacher must also remove or block on dependent assignments with a clear
message, with tests for both roles.

## 11. Classes and subjects screens
Goal: Admins can manage classes and subjects from the browser.
Description: Build list, create, edit and delete screens for both models in the same style as the teachers screens.
Validate the group values (`PRIME`, `SECONDE`, `TERZE`, `QUARTE`, `PRIMA4+2`) and the subject enumerations, and show how
many assignments depend on each item before deletion.

## 12. Assignments and pairings screens
Goal: Admins can manage assignments (with hours delivered and remaining) and class pairings.
Description: Build the screens for assignments, showing total and remaining hours, and for pairings, enforcing that both
classes have an assignment for the chosen subject (§5.4). Editing the hours must go through a form field that is
recorded as an adjustment once the ledger exists, so keep it behind a single function.

## 13. Calendar screens
Goal: Admins can view and edit the annual calendar by day and group.
Description: Build a month view of the calendar with colour coding for closures, stage days and short days, and forms to add
or edit a date, including a single-class stage chosen from a class menu. Show the resolved hours per group using the
function from task 9.

## 14. CSV import parsing and validation
Goal: The six setup CSV formats are parsed and validated by pure functions.
Description: Implement parsers for calendar, teachers, classes, subjects, assignments and pairings with the columns in
§5.1, and a cross-file validator producing errors and warnings as in §5.4. The functions take file contents and return
plain data and messages, with tests for each rule and for malformed input.

## 15. Calendar PDF parser
Goal: The annual calendar PDF is converted into calendar rows.
Description: Port the pdfplumber parser from §5.2, with the column geometry for each group in a named constant, stage
detection at 8 or more hours, and generation of closure rows for missing weekdays. Test it with a synthetic PDF built in
the test, and raise a clear error when no date row is recognised.

## 16. Setup wizard
Goal: An admin can upload the setup files, review problems and save everything in one step.
Description: Build the `/setup` screens that accept the files, show a preview with errors and warnings from tasks 14 and
15, allow live corrections and manual date addition, and save in one transaction. Keep the parked uploads in the database
rather than process memory so a restart does not lose them, and cover the save and the rollback with tests.

## 17. Availability model and inheritance
Goal: Weekly availability grids are stored and resolved with the inheritance rules.
Description: Add the availability model holding the day and time grid (§6) with validation of its shape, and a function
that returns a teacher's grid for a week: stored record, then the full-availability default for first-year teachers,
then the latest previous week, then the default. Also provide the weekly completion status calculation, with tests for
each branch.

## 18. Availability admin screen
Goal: Admins and secretariat can fill in the weekly availability grids.
Description: Build the week selector, the teacher-by-day-by-hour tap grid, "copy from previous week", and the completion
bar from the status function. Both roles can use it, saving goes through HTMX, and tests check the permissions and that
copying skips teachers already filled in.

## 19. Magic-link tokens
Goal: Admins can generate a one-time-view link for a teacher and a week.
Description: Add the link model storing only a hash of a random token, with teacher, week and expiry, and the admin action
that creates one and shows the URL once. Include revocation and a rate limit on token lookups, and make sure the token is
never written to logs in full.

## 20. Teacher availability page via link
Goal: A teacher can set their availability from a phone using only a link.
Description: Build the mobile-first tap grid page opened by a valid token, which reads and writes the same availability
record as the admin screen for that teacher and week only. Expired, revoked or unknown tokens show a neutral error and the
page grants access to nothing else.

## 21. Timetable and slot models with version counter
Goal: A weekly timetable and its lessons can be stored, and every change bumps a version.
Description: Add the timetable model (week, state `BOZZA`/`APPROVATO`, quality score and level, soft-conflict count,
version) and the lesson slot model as in §3. Provide one service function that applies a change inside a transaction and
increments the version, with a test showing concurrent-looking changes each bump it exactly once.

## 22. Solver core: context and hard constraints
Goal: A framework-free solver builds a feasible weekly timetable from plain input.
Description: Create the solver package with dataclasses for the input context (§9.1) and a CP-SAT model implementing
hard constraints H1, H1b, H2, H2b, H2c, H3, H5, H6 and H7/H8 (§9.2). Test each constraint on a small hand-built case, and
confirm the package imports nothing from Django.

## 23. Solver: quasi-hard constraints
Goal: Availability, Friday start, practical blocks and single-class limits are modelled with the relax weight.
Description: Add H4, H9, H10 and H11 (§9.3) as constraints with override variables at the relax weight of 1000, each
reported as a conflict when violated and switchable off by a derogation flag in the input. Test that each constraint
holds when possible and is reported when forced.

## 24. Solver: soft preferences and quality score
Goal: The solver optimises preferences with adjustable weights and returns a quality score.
Description: Add S1 to S5 with the default weights and the contractor scarcity multiplier (§9.4, §9.5), accepting a
weight map as input. Compute the quality score and level as in §9.6, and test the weights' priority order against the
1500, 1000 and 500 fixed weights.

## 25. Solver: fallback chain and conflict list
Goal: An infeasible week still returns the best possible timetable, with conflicts explained.
Description: Implement the rigid, best-effort and forced modes of §9.7, the fixed-lesson re-evaluation used for manual
edits, and the conflict listing with stable keys of §9.8. Test the transition from rigid to best-effort on an
infeasible case and the key stability across recomputation.

## 26. Context builder from the database
Goal: One function turns database state into the solver's input for a given week.
Description: Read assignments with remaining hours, availability (with inheritance and the rule that contractors with no
grid are unavailable), calendar resolution, pairings and teacher types, and compute the weekly targets from the hours
helper. Write it as a function returning the dataclass of task 22, tested with database fixtures including a stage week.

## 27. Solve jobs and worker
Goal: Generating a timetable runs in a background worker and reports its status.
Description: Add the job model and a `django.tasks` task using the database backend that builds the context, runs the
solver with the configured timeout, and writes slots, score and conflicts in one transaction with a version bump.
Failures and timeouts mark the job failed with a message, and a management command or service entry starts the worker.

## 28. Generate action and timetable grid view
Goal: An admin can generate a week and see the resulting grid, which refreshes when it changes.
Description: Build the week selector, the Generate button that enqueues a job and polls its status with HTMX, and the
read-only grid of classes by hour per day with stage cells and conflict highlighting. The page polls the timetable
version and refetches only on change; the secretariat sees the grid without any action buttons.

## 29. Move a lesson
Goal: An admin can drag a lesson to another slot, with the server validating the move.
Description: Add drag and drop with SortableJS that posts the move, re-evaluates the week with the fixed-lesson solve of
task 25, and returns the updated grid with new conflicts and score. Reject moves on an approved week and invalid hours or
days, and test the endpoint, not the JavaScript.

## 30. Assign a free slot and handle conflicts
Goal: Admins can fill an empty hour and approve or reject individual conflicts.
Description: Add the list of assignable teacher-subject pairs for a class, the assign endpoint that evaluates the forced
placement, and the conflict decisions: approve (stop reporting) and reject (free the hour). Decisions are stored and
cleared on regeneration as in §10, with tests for the paired-class case.

## 31. Quick actions (derogations)
Goal: Admins can apply a one-time derogation and regenerate.
Description: Add the seven quick actions of §9.7 with optional class, teacher, day and maximum hours, passed to the solver
for a single regeneration and not persisted. Show the suggested action next to each conflict, and write each use to the
audit log once task 39 exists, or to a placeholder service until then.

## 32. Explainable infeasibility
Goal: An infeasible week returns messages that say which resources are missing.
Description: Model the hard constraints that cause infeasibility with assumption literals, extract an unsat core on
failure within a time budget, and translate it into Italian sentences naming class, day, teachers and missing capacity.
Fall back to the generic message when the budget is exceeded, and test the translator on crafted infeasible weeks.

## 33. Repair solving
Goal: An absence or removal changes only a few lessons of an existing timetable.
Description: Add a solver mode that takes the current timetable and a perturbation (teacher absent on given days, slot
removed, class closed) and adds a penalty for every lesson that moves. Expose it as a job returning a diff against the
current timetable, shown for confirmation before being applied, with tests that the number of moved lessons is small.

## 34. Alternatives and pairwise choice
Goal: Generation can offer up to three alternatives and record which one the admin prefers.
Description: Produce alternatives by varying the soft weights in controlled ways, show a compact comparison of scores
and conflict kinds, and store the admin's pick together with the alternatives it beat. The data model must keep
enough evidence for task 35 and must not change any weight by itself.

## 35. Weight proposals
Goal: Consistent choices turn into weight proposals that an admin accepts or resets.
Description: Compute proposals from the stored picks as described in `architecture.md` §8, bounded at +50% per step and
never above the maximum weight of 200. Add the screen to apply all or some proposals or reset to default, with an
append-only adjustment history that the solver reads to get the active weights.

## 36. Hours ledger and approval
Goal: Approving a week records the delivered hours and locks the timetable.
Description: Add the append-only ledger model and a single `approve_timetable()` function, forward-only and admin-only,
that writes one entry per lesson (and per class for paired lessons), sets the state and approval time, and logs the
action in one transaction. Make it idempotent, count lessons without an hours row separately, and test the numbers
returned.

## 37. Undo approval and manual hours adjustments
Goal: A mistaken approval can be reversed and hours can be corrected with a trace.
Description: Add an undo function that writes compensating ledger entries and returns the week to draft, and route the
hours field of the assignments screen through adjustment entries. Both require a reason, are admin-only, and are covered
by tests that remaining hours return to their previous values.

## 38. PDF export
Goal: A timetable can be exported as a one-page A4 landscape PDF with the school's layout.
Description: Port the layout of §12: title band, class ordering by course and year, the colour per course and year, the
rotated day labels, the surname-only cells and yellow "STAGE" cells. Provide a download view that works in any state, and
test the structure by rendering and reading back the text.

## 39. Excel export and audit log
Goal: The same layout exports to Excel, and every sensitive action is recorded.
Description: Port the Excel export of §12 with merged cells, frozen panes and fit-to-page printing. Also add the audit-log
model and a single function that records generation, edits, quick actions, approval and undo, plus a read-only admin screen
listing the entries.

## 40. iCalendar feeds
Goal: Teachers and classes can subscribe to their approved timetable from a calendar app.
Description: Add one read-only feed per teacher and per class at an unguessable URL, generated on request from approved
weeks only, with correct local times and stage days omitted. Admins can view and regenerate a feed URL, and tests parse
the output and check that drafts are never included.

## 41. Draft model and calendar PDF vision reader
Goal: An optional AI reader proposes calendar rows from a PDF, stored as a draft.
Description: Add the draft model and an `ai/` function that sends a PDF to the vision model and returns plain calendar rows,
used only when the deterministic parser finds nothing or the admin asks. The result appears in the setup preview as a
draft, the function is mocked in every test, and with no API key the option is absent.

## 42. Natural-language instructions
Goal: An admin can type an Italian instruction and get a derogation to confirm.
Description: Add an `ai/` function mapping a sentence and the school context to a structured derogation or exclusion
(for example "Rossi non può il giovedì"), shown as a confirmation card and stored as a draft. Nothing is applied until the
admin confirms, ambiguous input asks for clarification, and tests cover the mapping with the API call mocked.

## 43. Production containers and proxy
Goal: The app deploys with one command on the small VM.
Description: Write the Dockerfile and `docker-compose.yml` with Postgres, the web server, the worker and Caddy for automatic
HTTPS, with every setting supplied through the environment file. Include the scripts to first start, update and create users,
and document the swap file needed at 1 GB of RAM.

## 44. Backups and restore
Goal: Database backups run automatically and a restore has been tested.
Description: Add a nightly local dump script with retention, an optional weekly off-site copy that is only configured by
environment variables, and a restore script. Document the procedure and test restoring a dump into an empty database.

## 45. Import from the existing system
Goal: Data from the running FastAPI system moves into the new database.
Description: Write a management command that reads the old Postgres tables listed in §3 and creates the new records,
converting delivered hours into opening ledger entries and mapping the old admin accounts to users with roles. Make it
repeatable on an empty target, report counts per table, and verify them against the source.

## 46. Solver performance benchmark
Goal: A repeatable benchmark guards the solver against slowdowns on real-sized data.
Description: Build a script that generates or loads a school-sized week, runs the solver, and records solve time, status and
score. Add a test with a loose time threshold for the normal case so a regression in model size or constraint cost is caught
before it reaches the 60-second timeout.
