# Deploy

One VM, Docker and Docker Compose, four containers: `postgres`, `web`
(gunicorn), `worker` (`manage.py solve_worker`) and `caddy` (HTTPS, static
files, reverse proxy). Only Caddy publishes ports (80, 443).

## Configuration

    cp deploy/env.production.example .env.production

Fill in `DOMAIN`, `SECRET_KEY`, `ALLOWED_HOSTS` and `POSTGRES_PASSWORD`
(`openssl rand -hex 24`: the password goes in a URL, so only letters, digits
and `. _ ~ -` are accepted). Every variable is commented in the template.
The DNS record of `DOMAIN` must point at the VM and ports 80 and 443 must be
open (also in the cloud provider's security list), or Caddy cannot obtain the
certificate.

For manual `docker compose` commands run `export COMPOSE_ENV_FILES=.env.production`
first; the scripts do it themselves.

## Swap file (1 GB machine)

Create a swap file of 2-4 GB (4 GB recommended):

    sudo fallocate -l 4G /swapfile
    sudo chmod 600 /swapfile
    sudo mkswap /swapfile
    sudo swapon /swapfile
    echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab

Verify with `swapon --show` and `free -h`. Optionally make the kernel prefer
RAM: `echo 'vm.swappiness=10' | sudo tee /etc/sysctl.d/99-swappiness.conf`
and `sudo sysctl --system`.

## Building on 1 GB

The image build is slow (tens of minutes: ortools and weasyprint wheels) and
can run out of memory. Either build on another machine and push or load the
image (`docker save` / `docker load`), or accept the slow build with swap on.

## First start

    deploy/up.sh

It checks the env file, builds, starts Postgres, runs `migrate` and
`collectstatic`, then starts everything. Running it again is safe and never
removes data.

## Update

    deploy/update.sh

Runs `git pull --ff-only` first (if it fails nothing is touched), then
build, migrate, collectstatic, restart, and removes dangling images.

## Users

    deploy/create-user.sh mario@example.com SEGRETERIA

The password is prompted (hidden). Roles: `ADMIN`, `SEGRETERIA`. An unknown
role, a malformed email, mismatching or too short passwords fail with an
error. An existing email is not an error: its password and role are reset
and the command says so. Non-interactive: `CREATE_USER_PASSWORD=... deploy/create-user.sh <email> <role>`.

A forgotten password is reset with

    docker compose exec web python manage.py changepassword <email>

## Logs

    docker compose logs -f web worker caddy

Note: until the token-redaction work lands, the Caddy and gunicorn access
logs record magic-link paths (`/l/<token>/`).

## Backups and restore

Dumps are made on the VM host through the `postgres` container
(`docker compose exec -T postgres pg_dump`), so no Postgres client is needed.
They are `pg_dump --format=custom` files (already compressed), checked with
`pg_restore --list` before they are kept.

- Where: `BACKUP_DIR` (default `./backups` in the repository, git-ignored),
  files `gestione-docenti-YYYYmmdd-HHMMSS-<pid>-<random>.dump`. The dump is
  written as `.partial` and renamed only after verification; a failed run
  removes it and prunes nothing.
- Retention: after a successful dump only the newest `BACKUP_KEEP` (default
  14, positive integer) dumps are kept, the one just written always among
  them. Other files in the directory are never touched.
- Manual backup: `deploy/backup.sh` (exit 0 and a "backup written" line on success).
- Schedule: `deploy/cron.example` runs the backup nightly (02:30) and the
  off-site copy weekly (Sunday 03:30), appending to `backups/cron.log`.
  Install: `mkdir -p backups`, `crontab -e`, paste the two lines with the
  right path. Check it ran: `ls -lt backups | head` (newest dump should be from
  last night) and `tail backups/cron.log`.

### Off-site copy

`deploy/backup-offsite.sh` copies the newest dump with `rclone` (installed on the
host, nothing else is installed or added to the image). It prints
`off-site not configured, skipped` and exits 0 unless `OFFSITE_RCLONE_REMOTE`
(e.g. `oracle:bucket/path`) is set in `.env.production`. Credentials: either
`OFFSITE_RCLONE_CONFIG` (path of an rclone config file) or standard
`RCLONE_CONFIG_<REMOTE>_*` variables in the same file (for example
`RCLONE_CONFIG_ORACLE_TYPE=s3`, `..._PROVIDER`, `..._ACCESS_KEY_ID`,
`..._SECRET_ACCESS_KEY`, `..._ENDPOINT`). They are passed to rclone through
its environment, never on a command line. A missing `rclone`, a failed copy
or an empty `BACKUP_DIR` exits non-zero.

**The bucket must be private**: the dumps contain personal data of teachers.
Expiry of old off-site copies is set with a lifecycle rule on the bucket.

### Restore

    docker compose stop web worker
    deploy/restore.sh backups/gestione-docenti-<...>.dump
    docker compose start web worker

then log in through the login page. `restore.sh` refuses a missing, unreadable
or non-dump file, and refuses a database that already has tables (nothing is
changed). To replace the current content pass `--yes-overwrite`: this is
**destructive**, it drops and recreates the `public` schema first. To restore
into a really empty database instead, drop and recreate it first, or use
`docker compose down` and `docker volume rm` of `pgdata` (only if you mean it),
then `deploy/up.sh`, which migrates, and `--yes-overwrite`. If `pg_restore`
stops on an error the database may be half restored: empty it and retry.
After a restore `manage.py migrate` reports `No migrations to apply`.

### Restore test

`deploy/test-restore.sh` starts a throwaway `postgres:15-alpine` container
(never the real database), migrates a database, creates a user, an assignment
and ledger entries, dumps with `backup.sh`, restores with `restore.sh` into a
second empty database, compares count and a hash of the rows of every table,
runs `migrate` and a login, and removes the container. Needs docker and uv.
`tests/test_backup_scripts.py` covers retention, failures, off-site and the
restore refusals with a fake docker and rclone.

Result: 2026-10-16, `deploy/test-restore.sh`, PASSED (32 tables identical,
migrate clean, login works).

Not verified without a real server: that the real bucket receives the weekly
copy, the schedule firing at the real hour and surviving a reboot, behaviour
and disk use on the 1 GB VM.

## Data

Postgres data lives in the `pgdata` volume. `docker compose down` keeps it;
never use `down -v` unless you mean to delete the database.
