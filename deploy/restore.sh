#!/usr/bin/env bash
# Restore a dump (made by backup.sh) into the database. Refuses a database that already has tables.
# Usage: deploy/restore.sh <dump> [--yes-overwrite]
# --yes-overwrite DESTROYS the current content of the database (drops the public schema first).
set -euo pipefail
# shellcheck source=deploy/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
cd "$ROOT"

fail() { echo "restore FAILED: $*" >&2; exit 1; }

dump=""
overwrite=0
for arg in "$@"; do
    case "$arg" in
        --yes-overwrite) overwrite=1 ;;
        -*) fail "unknown option $arg. Usage: deploy/restore.sh <dump> [--yes-overwrite]" ;;
        *) [ -z "$dump" ] || fail "only one dump file can be given"; dump="$arg" ;;
    esac
done
[ -n "$dump" ] || fail "usage: deploy/restore.sh <dump> [--yes-overwrite]"
[ -e "$dump" ] || fail "$dump does not exist"
[ -f "$dump" ] || fail "$dump is not a regular file"
[ -r "$dump" ] || fail "$dump is not readable"
pg_exec pg_restore --list < "$dump" > /dev/null 2>&1 \
    || fail "$dump is not a valid pg_dump custom-format file (or the postgres container is not running)"

sql() { pg_exec psql -U "$PG_USER" -d "$PG_DB" -v ON_ERROR_STOP=1 -tA "$@"; }

tables="$(sql -c "select count(*) from information_schema.tables where table_schema = 'public' and table_type = 'BASE TABLE'")" \
    || fail "cannot query the database $PG_DB"
if [ "$tables" != "0" ]; then
    if [ "$overwrite" != "1" ]; then
        fail "database $PG_DB already has $tables table(s). Nothing was changed. To replace its content (destructive) pass --yes-overwrite."
    fi
    echo "dropping and recreating the public schema of $PG_DB (--yes-overwrite)"
    sql -c "drop schema public cascade; create schema public;" > /dev/null
fi

pg_exec pg_restore --exit-on-error --no-owner --no-privileges -U "$PG_USER" -d "$PG_DB" < "$dump" \
    || fail "pg_restore stopped on an error: the database may be half restored, empty it and retry"
echo "restored $(basename "$dump") into $PG_DB"
