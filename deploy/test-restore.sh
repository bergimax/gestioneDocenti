#!/usr/bin/env bash
# Restore test on a throwaway Postgres container: never touches the real database.
# Migrates a database, inserts a user, an assignment and ledger entries, dumps it with backup.sh,
# restores the dump into a second empty database with restore.sh, then compares every table
# (row count and a hash of all rows), runs `migrate` and a login on the restored database.
# Needs docker and uv on the host. Usage: deploy/test-restore.sh
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

name="gd-restore-test-$$"
work="$(mktemp -d)"
port="${TEST_RESTORE_PORT:-55432}"
cleanup() {
    docker rm -f "$name" > /dev/null 2>&1 || true
    rm -rf "$work"
}
trap cleanup EXIT

docker run -d --name "$name" -e POSTGRES_PASSWORD=restoretest -p "127.0.0.1:$port:5432" postgres:15-alpine > /dev/null
for _ in $(seq 1 30); do
    docker exec "$name" pg_isready -U postgres > /dev/null 2>&1 && break
    sleep 1
done
psql_admin() { docker exec -i "$name" psql -U postgres -v ON_ERROR_STOP=1 -tA "$@"; }
# a second pg_isready: the init phase restarts the server once
sleep 2
psql_admin -c "create database src_db" -c "create database dst_db" > /dev/null

url() { echo "postgres://postgres:restoretest@127.0.0.1:$port/$1"; }
managepy() {
    local db="$1"
    shift
    DATABASE_URL="$(url "$db")" SECRET_KEY=restore-test DEBUG=false DOMAIN=localhost \
        ALLOWED_HOSTS=localhost,testserver uv run manage.py "$@"
}

managepy src_db migrate --noinput > /dev/null
managepy src_db shell -c '
import datetime
from accounts.models import User
from ledger.models import EntryKind, LedgerEntry
from school.models import Assignment, Class, School, Subject, Teacher
u = User.objects.create_user("restore@example.com", "pw-long-enough-1", "ADMIN")
s = School.objects.create(name="S", formative_year="2026/27", year_start=datetime.date(2026, 9, 14), year_end=datetime.date(2027, 6, 12))
t = Teacher.objects.create(school=s, name="Rossi Mario", type="ASSUNTO")
c = Class.objects.create(school=s, name="1A", student_count=20)
sub = Subject.objects.create(school=s, name="Italiano", kind="TEORIA")
a = Assignment.objects.create(school=s, teacher=t, school_class=c, subject=sub, ore_totali=40)
for h in (2, 3, 1):
    LedgerEntry.objects.create(assignment=a, hours=h, kind=EntryKind.DELIVERED, week=datetime.date(2026, 10, 12), created_by=u)
' > /dev/null

# backup.sh and restore.sh run against the throwaway container
printf 'POSTGRES_USER=postgres\nPOSTGRES_DB=%s\nBACKUP_DIR=%s\nBACKUP_KEEP=3\n' src_db "$work/backups" > "$work/src.env"
printf 'POSTGRES_USER=postgres\nPOSTGRES_DB=%s\n' dst_db > "$work/dst.env"
export PG_CONTAINER="$name"
ENV_FILE="$work/src.env" deploy/backup.sh
dump="$(find "$work/backups" -name 'gestione-docenti-*.dump' | head -n 1)"
ENV_FILE="$work/dst.env" deploy/restore.sh "$dump"

# every table of both databases: row count and a hash of the rows
snapshot() {
    local db="$1" table
    for table in $(psql_admin -d "$db" -c "select tablename from pg_tables where schemaname = 'public' order by 1"); do
        echo "$table $(psql_admin -d "$db" -c "select count(*), coalesce(md5(string_agg(t::text, '|' order by t::text)), '') from \"$table\" t")"
    done
}
snapshot src_db > "$work/src.snap"
snapshot dst_db > "$work/dst.snap"
if ! diff "$work/src.snap" "$work/dst.snap"; then
    echo "restore test FAILED: tables differ" >&2
    exit 1
fi
tables="$(wc -l < "$work/src.snap")"
for table in accounts_user school_assignment ledger_ledgerentry; do
    grep -qE "^$table [1-9]" "$work/dst.snap" || { echo "restore test FAILED: $table is empty after restore" >&2; exit 1; }
done

migrate_out="$(managepy dst_db migrate --noinput 2>&1)"
echo "$migrate_out" | grep -q "No migrations to apply" || { echo "restore test FAILED: migrate on the restored database: $migrate_out" >&2; exit 1; }

managepy dst_db shell -c '
from django.test import Client
c = Client()
r = c.post("/login/", {"username": "restore@example.com", "password": "pw-long-enough-1"})
assert r.status_code == 302, r.status_code
assert c.get("/").status_code == 200
' > /dev/null || { echo "restore test FAILED: login on the restored database" >&2; exit 1; }

echo "restore test PASSED: $tables tables identical, migrate clean, login works"
