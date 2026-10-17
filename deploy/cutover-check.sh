#!/usr/bin/env bash
# Read-only cut-over checks; changes nothing. Usage: deploy/cutover-check.sh [--local]
set -euo pipefail
# shellcheck source=deploy/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

usage() {
    echo "Usage: $0 [--local]"
    echo "Read-only cut-over checks. --local: rehearsal on a throwaway stack (CHECK_URL, default http://localhost)."
}

local_mode=0
for arg in "$@"; do
    case "$arg" in
        --help | -h) usage; exit 0 ;;
        --local) local_mode=1 ;;
        *) echo "Unknown argument: $arg" >&2; usage >&2; exit 2 ;;
    esac
done
cd "$ROOT"

failures=0
ok() { echo "OK   $1"; }
fail() { echo "FAIL $1"; failures=$((failures + 1)); }

domain=""
if [ -f "$ENV_FILE" ]; then
    domain="$(env_value DOMAIN)"
fi

# 1. DOMAIN
if [ "$local_mode" = 1 ]; then
    ok "1 DOMAIN check skipped (--local)"
elif [ -z "$domain" ]; then
    fail "1 DOMAIN is empty or $ENV_FILE is missing"
elif [ "$domain" = "localhost" ]; then
    fail "1 DOMAIN is localhost: set the real host name in $ENV_FILE"
else
    ok "1 DOMAIN is set ($domain)"
fi

# 2. DNS
if [ "$local_mode" = 1 ]; then
    ok "2 DNS check skipped (--local)"
elif [ -z "$domain" ] || [ "$domain" = "localhost" ]; then
    fail "2 no real DOMAIN to resolve"
elif resolved="$(getent hosts "$domain" | awk '{print $1}' | paste -sd ' ' -)" && [ -n "$resolved" ]; then
    ok "2 $domain resolves to $resolved"
else
    fail "2 $domain does not resolve (DNS not switched yet, or not propagated)"
fi

# 3 and 4. HTTPS answer and login form
if [ "$local_mode" = 1 ]; then
    base="${CHECK_URL:-http://localhost}"
    curl_opts=(-sS -L -k --max-time 20)
else
    base="${CHECK_URL:-https://$domain}"
    curl_opts=(-sS -L --max-time 20)
fi
if [ -z "${domain:-}" ] && [ "$local_mode" = 0 ] && [ -z "${CHECK_URL:-}" ]; then
    fail "3 no URL to check (DOMAIN empty)"
    fail "4 login page not checked (no URL)"
else
    code=""
    if code="$(curl "${curl_opts[@]}" -o /dev/null -w '%{http_code}' "$base/" 2> /dev/null)"; then
        if [ "$code" -ge 100 ] && [ "$code" -lt 500 ]; then
            ok "3 $base/ answers $code"
        else
            fail "3 $base/ answers $code"
        fi
    else
        fail "3 $base/ not reachable or certificate not valid (curl failed)"
    fi
    if page="$(curl "${curl_opts[@]}" "$base/login/" 2> /dev/null)" \
        && grep -q 'name="password"' <<< "$page"; then
        ok "4 login page at $base/login/ contains the login form"
    else
        fail "4 login page at $base/login/ missing or without the login form"
    fi
fi

# 5. containers
if running="$(dc ps --status running --services 2> /dev/null)"; then
    missing=""
    for svc in postgres web worker caddy; do
        grep -qx "$svc" <<< "$running" || missing="$missing $svc"
    done
    if [ -z "$missing" ]; then
        ok "5 postgres, web, worker, caddy are running"
    else
        fail "5 not running:$missing"
    fi
else
    fail "5 docker compose ps failed (stack down or docker missing)"
fi

# 6. migrations
if dc exec -T web python manage.py migrate --check > /dev/null 2>&1; then
    ok "6 no migrations to apply"
else
    fail "6 migrate --check reports unapplied migrations (or web is not reachable)"
fi

# 7. data present
counts="$(dc exec -T web python manage.py shell -c '
from accounts.models import User
from school.models import Assignment
print(User.objects.count(), Assignment.objects.count())
' 2> /dev/null | tail -n 1)" || counts=""
if [[ "$counts" =~ ^([0-9]+)\ ([0-9]+)$ ]]; then
    if [ "${BASH_REMATCH[1]}" -ge 1 ] && [ "${BASH_REMATCH[2]}" -ge 1 ]; then
        ok "7 database has ${BASH_REMATCH[1]} user(s) and ${BASH_REMATCH[2]} assignment(s)"
    else
        fail "7 database has ${BASH_REMATCH[1]} user(s) and ${BASH_REMATCH[2]} assignment(s): empty or not imported"
    fi
else
    fail "7 could not count users and assignments (web not reachable)"
fi

if [ "$failures" -gt 0 ]; then
    echo "$failures check(s) FAILED"
    exit 1
fi
echo "all checks OK"
