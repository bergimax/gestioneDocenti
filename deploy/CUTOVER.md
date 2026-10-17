# Production cut-over runbook

One-off move from the running FastAPI system (old: `backend`, `frontend`,
`postgres`, `caddy`) to the Django deployment in this repository (new:
`postgres`, `web`, `worker`, `caddy`). It is done once, by a person, following
this document. Nothing in it is automated: no script edits DNS or freezes the
old system. The real cut-over is issue #82; the old system is removed in #83.

**Open question (ask the owner before the rehearsal):** do the old and the new
system run on the same VM or on two VMs? Nothing in the repo or in `_docs/`
says so (`specs.md` section 15 describes only the current single Oracle VM).
Both variants are written below. Passages marked **(same VM)** and
**(two VMs)** apply to one case only; mark the one that does not apply "not
applicable" in your copy of the plan. Related open point: the number of hours
after which a rollback stops being simple (see Rollback) is a proposal, **to
confirm**.

All commands run from the repository root of the NEW system. `dc` below means
`docker compose` with the environment file exported once per shell:

    export COMPOSE_ENV_FILES=.env.production
    alias dc='docker compose --env-file .env.production'

`<user>`, `<password>`, `<host>`, `<db>`, `<login>` are placeholders. Never
write a real value of them into this file, a ticket, or a chat.

## Overview and timeline

The order is: rehearse, freeze the old system, final dump, import, set
passwords, check, switch the domain, verify. Durations are estimates **to
confirm in the rehearsal**.

| # | Step | Who | Where | Duration | Point of no return |
|---|------|-----|-------|----------|--------------------|
| 1 | Prerequisites checklist, backup of the empty new database | operator | new VM | 30 min | no |
| 2 | Lower the DNS TTL (only if the name changes) | operator | DNS | 5 min, wait one old TTL | no |
| 3 | Rehearsal (repeat until it passes) | operator | new VM, copy of the old data | 30 min per run | no |
| 4 | Announce the freeze to users | school admin | out of band | days before | no |
| 5 | Freeze the old system | operator | old VM | 5 min | no: unfreeze by starting the old containers |
| 6 | Final dump, check, copy off the VM | operator | old VM, then new VM | 10 min | no |
| 7 | Import: dry run, then real run | operator | new VM | 5 to 15 min | no: a failed import writes nothing |
| 8 | Go / no-go decision on the report | school admin and operator | new VM | 15 min | no |
| 9 | Set passwords, check two examples, backup of the imported database | operator | new VM | 30 min | no: restore the empty backup |
| 10 | Switch the domain | operator | DNS (or new VM) | 5 min, plus propagation | **yes, from here the old system no longer receives users** |
| 11 | Verify (`deploy/cutover-check.sh` and manual checks) | operator | new VM, browser | 15 min | no |
| 12 | Users work in the new system; keep the list of edits | school admin | new system | first 24 h | after N hours (see Rollback) |

Downtime is the time between steps 5 and 10 (about 2 hours with a prepared
operator); it is measured in #82, not here.

The point of no return in practice is the moment users start editing in the
new system after step 10: from then on a rollback loses their edits.

## Prerequisites

Tick every line before the freeze.

- [ ] The new stack is up as described in `deploy/README.md`, with
      `.env.production` filled in, `deploy/up.sh` run, migrations applied.
      **(same VM)** The old Caddy owns ports 80 and 443, so the new `caddy`
      cannot start yet. Do not use `deploy/up.sh` until the old stack is
      stopped; instead start everything but Caddy:

          dc build
          dc up -d --wait postgres
          dc run --rm --no-deps web python manage.py migrate --noinput
          dc run --rm --no-deps web python manage.py collectstatic --noinput
          dc up -d web worker

      A 1 GB VM running both stacks needs the swap file of `deploy/README.md`.
      **(two VMs)** Just run `deploy/up.sh`.
- [ ] The new database is still EMPTY: no assignments, no timetables. A first
      admin may exist. (`import_legacy` refuses a non-empty target.)
- [ ] Backups work (see "Backups and restore" in `deploy/README.md`) and a fresh
      backup of the new empty database exists. It is the restore point for a
      failed import:

          deploy/backup.sh
          ls -t backups | head -1     # note this file name: <empty-backup>

- [ ] A person is named who decides go / no-go and rollback (the school admin
      or the project owner), and a second person can be reached.
- [ ] The network path from the new VM to the old database is decided.
      **Variant A, SSH tunnel** to the old Postgres (it must be reachable
      from the new web container; a tunnel bound only to the VM's loopback is
      not). **Variant B, dump copy (recommended, same for both layouts)**:
      take a dump of the old database, load it into a throwaway Postgres on
      the new VM, import from there. Commands for B:

      # on the old VM (service names as in the old docker-compose.yml)
      docker compose exec -T postgres pg_dump -U <user> --format=custom <db> > legacy.dump
      docker compose exec -T postgres pg_restore --list < legacy.dump > /dev/null && echo dump ok

      # copy legacy.dump to the new VM (scp), then on the new VM
      docker run -d --name legacy-scratch -e POSTGRES_PASSWORD=<scratch-password> postgres:15-alpine
      docker exec legacy-scratch pg_isready -U postgres
      docker exec legacy-scratch createdb -U postgres legacy
      docker exec -i legacy-scratch pg_restore --no-owner -U postgres -d legacy < legacy.dump

      The import reads from that container. Attach it to the compose network
      so the web container can reach it by name (`docker network ls` shows the
      compose network, usually `<directory>_default`):

          docker network connect <compose-network> legacy-scratch

      The source URL is then `postgres://postgres:<scratch-password>@legacy-scratch:5432/legacy`.
- [ ] The source URL contains a password. It is given to the import only by the
      environment variable `IMPORT_SOURCE_URL` (or `--source`), typed with the
      shell reading it hidden, never pasted into a document, a ticket or an
      example with real values:

          read -rs IMPORT_SOURCE_URL && export IMPORT_SOURCE_URL

      and removed at the end of every session with `unset IMPORT_SOURCE_URL`.

## Rehearsal

Do this first, as often as needed. It uses a COPY of the old database and a
scratch target, changes no DNS, and leaves the old system untouched. Use a dump
of the old database (Variant B above) loaded into `legacy-scratch`.

    # scratch target database inside the new Postgres (user from POSTGRES_USER)
    dc exec -T postgres createdb -U <user> rehearsal
    read -rs DATABASE_URL && export DATABASE_URL   # postgres://<user>:<password>@postgres:5432/rehearsal
    dc run --rm --no-deps -e DATABASE_URL web python manage.py migrate --noinput

    # dry run, read the report
    dc run --rm --no-deps -e DATABASE_URL -e IMPORT_SOURCE_URL web python manage.py import_legacy --dry-run
    echo "exit code $?"

    # real run into the scratch database, then check the exit code
    dc run --rm --no-deps -e DATABASE_URL -e IMPORT_SOURCE_URL web python manage.py import_legacy
    echo "exit code $?"

    # clean up: drop the scratch database, forget the secrets
    dc exec -T postgres dropdb -U <user> rehearsal
    unset DATABASE_URL IMPORT_SOURCE_URL

A passing rehearsal looks like this: both runs exit with code 0, the report
begins with "DRY RUN" and then "Import done and verified", and every table has
no skipped row that you cannot explain (rules in "Import" below). Write down
the date, the command, and the per-table report of every rehearsal. A
rehearsal that does not pass is repeated after the cause is fixed (in the data
by a person, or in the importer by a new issue), never by editing the real
old database.

## Freeze the old system

Method (needs no change to the old code): stop the old `backend` and
`frontend` containers. Nobody can then edit, and the database stays as it is.
Optionally put a maintenance page in the old Caddy first.

1. Announce the freeze to users in advance (days, by the school's usual
   channel): from the stated time nobody edits in the old system, and the new
   address and the day for the new passwords are given.
2. At the stated time, on the old VM, in the old repository directory:

       docker compose stop backend frontend

   Leave the old `postgres` running. Check the old site no longer answers
   edits. **No one may edit after the freeze.**
3. Final dump, check, off-VM copy:

       docker compose exec -T postgres pg_dump -U <user> --format=custom <db> > legacy-final.dump
       docker compose exec -T postgres pg_restore --list < legacy-final.dump > /dev/null && echo dump ok
       sha256sum legacy-final.dump

   Copy `legacy-final.dump` off the VM (your own machine or the private
   off-site bucket; it holds personal data of teachers, so keep it private)
   and keep it until #83. Load it into the scratch container as in
   Prerequisites (drop the old rehearsal copy first:
   `docker exec legacy-scratch dropdb -U postgres legacy` and `createdb`).
4. **(same VM)** Also stop the old Caddy and the old Postgres once the dump is
   safe, to free ports 80 and 443: `docker compose stop caddy postgres`. Then
   start the new Caddy: `deploy/up.sh`.

Unfreeze (rollback of this step): `docker compose start postgres backend
frontend caddy` in the old directory.

## Import

Run it inside the new web container, on the real (empty) new database.
`IMPORT_SOURCE_URL` is set in the shell as in Prerequisites.

    dc exec -e IMPORT_SOURCE_URL web python manage.py import_legacy --dry-run
    echo "exit code $?"
    # read the report with the school admin, decide go / no-go, then:
    dc exec -e IMPORT_SOURCE_URL web python manage.py import_legacy
    echo "exit code $?"
    unset IMPORT_SOURCE_URL

(`dc run --rm --no-deps -e IMPORT_SOURCE_URL web python manage.py import_legacy`
works too.) The command exits 0 only when the data was imported and verified;
on exit 1 nothing was written and the new database is as before, so fix the
cause and run again.

**Reading the report.** One line per old table with three numbers: `source`
(rows in the old table), `created` (rows in the new system), `skipped` (rows
that could not be imported). A "Skipped rows" section lists each skipped row
with the old id and the reason; a "Notes" section lists rows that were
imported with a change (for example hours defaulted to 0, an unknown author
stored as empty). `orario_feedback` shows "not imported (no model yet)": the
ratings are not imported (#79); this is expected and does not block the
cut-over. `admin_session`, `chat_message`, `preferenze_ai_memory` and
`conflitto_gestito` are not imported by design.

**Go / no-go rule.** Go only if all hold:

1. the exit code is 0 (a real run: "Import done and verified");
2. for every imported table, `source - skipped = created` (the importer checks
   this itself; read the numbers anyway);
3. every skipped row is explained by one of: orphan (points to a deleted
   parent), duplicate, invalid week or hours; a skipped row that cannot be
   explained is a no-go;
4. every line under "Notes" is understood and accepted by the school admin.

What to do with skipped rows: a person (school admin) decides for each whether
it is rubbish that stays out or a real record to enter again by hand after the
cut-over, in the screens of the new system. Nothing is edited by hand in the
database without writing down what was changed and why.

No-go: do not switch the domain. Roll back as in Rollback (a) and unfreeze.

## After the import

1. Imported users have no password and cannot log in. Set one per user, on the
   new VM, with a password you generate for each:

       dc exec web python manage.py changepassword <login>

   (or `deploy/create-user.sh <login> <ADMIN|SEGRETERIA>`, which resets
   password and role of an existing user). The new password is told to each
   user out of band (in person or by phone): the system sends nothing.
2. Log in as an admin. Open an old approved timetable and one teacher's
   remaining hours. Compare with the old system (still readable: the old
   Postgres is running or the dump is loaded) for two named examples, for
   instance one teacher with a known total of hours and one class timetable of
   a named week. Write down which two.
3. Take a fresh backup of the imported database:

       deploy/backup.sh

   Note its file name: it is the restore point for the first hours after the
   switch.

## Switch the domain

Choose one.

**New name** (the new site gets its own host name): beforehand, lower the TTL
of the new name's A record (for example to 300 seconds) and wait one old TTL.
At the switch, change the A record of the new name to the IP of the new VM.
`DOMAIN` and `ALLOWED_HOSTS` in `.env.production` already hold that name.

**Old name reused** (users keep the old address): set `DOMAIN` and
`ALLOWED_HOSTS` in `.env.production` to the old name, then restart Caddy and
web with the new values:

    deploy/up.sh

and, **(two VMs)**, point the old name's A record at the new VM. **(same VM)**
the IP does not change: stopping the old Caddy and starting the new one (above)
is the switch.

Check propagation from a machine outside the school network:

    dig +short <domain>

It must print the IP of the new VM. Caddy obtains the certificate
automatically only when DNS points at the VM and ports 80 and 443 are open
(also in the cloud provider's security list); until then the browser shows a
certificate error. `DOMAIN` must be a real host name; `sslip.io` names work
only while they contain the VM's IP, so they cannot be reused for a different
VM. Watch Caddy while it gets the certificate:

    docker compose logs -f caddy

## Verify

    deploy/cutover-check.sh

It is read-only and prints one line per check, `OK` or `FAIL` with the cause,
and exits non-zero if any fails: `DOMAIN` is a real name; it resolves; HTTPS
answers with a valid certificate; the login page has the login form; the four
containers run; no migration is pending; the database has users and
assignments. Run it also before the switch (the DNS, HTTPS checks then FAIL as
expected) and after. On a throwaway stack for a rehearsal use
`deploy/cutover-check.sh --local` (`CHECK_URL` overrides the address).

Manual checks, in a browser:

- the HTTPS padlock shows, without a warning;
- the login page loads;
- one admin logs in with the password set in "After the import";
- the home page loads;
- one calendar feed URL (`/feeds/<token>.ics`) from the feeds page answers.

## Rollback

The person who decides is named in Prerequisites (school admin or project
owner). Rule: roll back if the go / no-go rule fails, or if after the switch
the new system cannot be used and cannot be fixed within the agreed time.

**(a) The import has not run, or exited non-zero.** Nothing was written. Fix
the cause and retry; if the cut-over is abandoned, unfreeze the old system:

    # on the old VM, old directory
    docker compose start backend frontend
    # (same VM) docker compose start postgres caddy backend frontend

The old system is exactly as frozen.

**(b) The import succeeded, DNS not yet switched.** Put the new database back
to its empty state and unfreeze the old system:

    docker compose stop web worker
    deploy/restore.sh backups/<empty-backup> --yes-overwrite
    docker compose start web worker

(`--yes-overwrite` is destructive: it drops the content of the new database.
The empty backup was taken in Prerequisites.) Then unfreeze the old system as
in (a). Passwords given to users are void with the database; do not tell users
to use the new system.

**(c) After the DNS switch.** Point the DNS record back at the old system
**(two VMs)**, or **(same VM)** stop the new stack (`docker compose stop caddy
web worker`) and start the old one (`docker compose start postgres backend
frontend caddy` in the old directory). Unfreeze the old system as in (a).
Say plainly to users: **everything edited in the new system after the switch
is NOT in the old system**. The old system is as it was at the freeze. Keep a
written list, from the first hour, of every edit made in the new system
(who, what, when); after a rollback these edits must be redone by hand in the
old system from that list. The new database is kept (do not delete the
volume) so nothing is lost for reference.

**Point after which rollback is no longer simple:** once users have worked in
the new system for N = 24 hours (**to confirm**), redoing their edits by hand
costs more than fixing forward; from then on prefer fixing the new system,
and roll back only for a data-loss problem. The decision is the school
admin's, with the operator's estimate of the number of edits on the list.

## Keep the old system

Do not delete the old system at the cut-over. Keep the old containers stopped
(not removed), the old Postgres volume, and the final dump `legacy-final.dump`
(with its off-VM copy and checksum) until #83 removes them after the waiting
period. Remove the scratch container when no more rehearsals are needed:

    docker rm -f legacy-scratch

Clear the shell secrets: `unset IMPORT_SOURCE_URL DATABASE_URL`.
