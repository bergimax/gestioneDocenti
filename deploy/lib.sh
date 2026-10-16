# Shared helpers, sourced by the deploy scripts. Defines functions only.
# shellcheck shell=bash

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-.env.production}"
export COMPOSE_ENV_FILES="$ENV_FILE"

# Value of NAME in the env file (last assignment, surrounding quotes removed).
env_value() {
    local line
    line="$(grep -E "^$1=" "$ENV_FILE" | tail -n 1 || true)"
    line="${line#*=}"
    line="${line%\"}"
    line="${line#\"}"
    line="${line%\'}"
    line="${line#\'}"
    printf '%s' "$line"
}

check_env() {
    if [ ! -f "$ENV_FILE" ]; then
        echo "Missing $ENV_FILE: copy deploy/env.production.example to it and fill it in." >&2
        exit 1
    fi
    local name
    for name in DOMAIN SECRET_KEY ALLOWED_HOSTS POSTGRES_PASSWORD; do
        if [ -z "$(env_value "$name")" ]; then
            echo "$name is empty or missing in $ENV_FILE." >&2
            exit 1
        fi
    done
    if ! [[ "$(env_value POSTGRES_PASSWORD)" =~ ^[A-Za-z0-9._~-]+$ ]]; then
        echo "POSTGRES_PASSWORD may only contain A-Z a-z 0-9 . _ ~ - (it is put in a URL). Try: openssl rand -hex 24" >&2
        exit 1
    fi
}

dc() {
    docker compose --env-file "$ENV_FILE" "$@"
}

# Build, migrate, collect static files, then start everything.
deploy_stack() {
    dc build
    dc up -d --wait postgres
    dc run --rm --no-deps web python manage.py migrate --noinput
    dc run --rm --no-deps web python manage.py collectstatic --noinput
    dc up -d
}

# Value of NAME: the process environment wins, then the env file, then DEFAULT.
setting() {
    local name="$1" default="${2:-}" value="${!1:-}"
    if [ -z "$value" ] && [ -f "$ENV_FILE" ]; then
        value="$(env_value "$name")"
    fi
    printf '%s' "${value:-$default}"
}

# Run a command inside the postgres container, stdin passed through, no TTY.
# PG_CONTAINER (a container name) replaces the compose service; the tests use it.
pg_exec() {
    if [ -n "${PG_CONTAINER:-}" ]; then
        docker exec -i "$PG_CONTAINER" "$@"
    else
        dc exec -T postgres "$@"
    fi
}

PG_USER="$(setting POSTGRES_USER gestione)"
PG_DB="$(setting POSTGRES_DB gestione_docenti)"

# Absolute backup directory (BACKUP_DIR, default ./backups under the repository).
backup_dir() {
    local dir
    dir="$(setting BACKUP_DIR ./backups)"
    case "$dir" in
        /*) printf '%s' "$dir" ;;
        *) printf '%s' "$ROOT/$dir" ;;
    esac
}

# Dump files in DIR written by backup.sh (never .partial or foreign files), newest name first.
list_dumps() {
    find "$1" -maxdepth 1 -type f -name 'gestione-docenti-[0-9]*-[0-9]*-*.dump' | sort -r
}
