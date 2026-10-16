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

## Data

Postgres data lives in the `pgdata` volume. `docker compose down` keeps it;
never use `down -v` unless you mean to delete the database.
