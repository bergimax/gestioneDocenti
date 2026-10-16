#!/usr/bin/env bash
# Usage: deploy/create-user.sh <email> <ADMIN|SEGRETERIA>
# The password is prompted by manage.py (hidden, not on any command line).
# Non-interactive: CREATE_USER_PASSWORD=... deploy/create-user.sh <email> <role>
set -euo pipefail
# shellcheck source=deploy/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
cd "$ROOT"

if [ "$#" -ne 2 ]; then
    echo "Usage: $0 <email> <ADMIN|SEGRETERIA>" >&2
    exit 2
fi
check_env

if [ -n "${CREATE_USER_PASSWORD:-}" ]; then
    # -e NAME without a value forwards it from this shell's environment.
    dc exec -T -e CREATE_USER_PASSWORD web sh -c \
        'exec python manage.py create_user "$1" --role "$2" --password "$CREATE_USER_PASSWORD"' \
        sh "$1" "$2"
else
    dc exec web python manage.py create_user "$1" --role "$2"
fi
