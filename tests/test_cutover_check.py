"""deploy/cutover-check.sh with a fake docker, curl and getent: no stack, no network."""

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "cutover-check.sh"

FAKE_DOCKER = """#!/usr/bin/env bash
echo "$*" >> "$FAKE_LOG"
args="$*"
case "$args" in
  *" ps "*) printf '%s\\n' ${FAKE_SERVICES:-postgres web worker caddy} ;;
  *"migrate --check"*) exit "${FAKE_MIGRATE_RC:-0}" ;;
  *"shell -c"*) echo "${FAKE_COUNTS:-3 12}" ;;
  *) exit 1 ;;
esac
"""

FAKE_CURL = """#!/usr/bin/env bash
echo "$*" >> "$FAKE_LOG"
[ -z "${FAKE_CURL_FAIL:-}" ] || exit 60
case "$*" in
  *"%{http_code}"*) printf '%s' "${FAKE_HTTP:-200}" ;;
  *) echo "${FAKE_PAGE:-<form><input name=\\"password\\"></form>}" ;;
esac
"""

FAKE_GETENT = """#!/usr/bin/env bash
[ -z "${FAKE_NO_DNS:-}" ] || exit 2
echo "203.0.113.7 $2"
"""


@pytest.fixture
def run(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (
        ("docker", FAKE_DOCKER),
        ("curl", FAKE_CURL),
        ("getent", FAKE_GETENT),
    ):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    env_file = tmp_path / "prod.env"

    def go(*args, domain="school.example.org", **extra):
        env_file.write_text(
            f"DOMAIN={domain}\nSECRET_KEY=fake-secret-key-xyz\n"
            "POSTGRES_PASSWORD=fake-pg-password-xyz\n"
        )
        env = {
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "ENV_FILE": str(env_file),
            "FAKE_LOG": str(tmp_path / "calls.log"),
            **extra,
        }
        return subprocess.run(
            [str(SCRIPT), *args], capture_output=True, text=True, env=env
        )

    go.log = tmp_path / "calls.log"
    return go


def test_executable_and_strict_mode():
    assert os.access(SCRIPT, os.X_OK)
    assert "set -euo pipefail" in SCRIPT.read_text().splitlines()[:4]
    assert "set -x" not in SCRIPT.read_text()
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0


def test_help_exits_zero(run):
    result = run("--help")
    assert result.returncode == 0 and "Usage" in result.stdout


def test_unknown_argument_fails_with_usage(run):
    result = run("--nope")
    assert result.returncode != 0 and "Usage" in result.stderr


def test_all_ok(run):
    result = run()
    assert result.returncode == 0, result.stdout
    assert "FAIL" not in result.stdout
    assert result.stdout.count("OK") >= 7
    assert "203.0.113.7" in result.stdout


def test_localhost_domain_fails(run):
    result = run(domain="localhost")
    assert result.returncode != 0
    assert "FAIL 1" in result.stdout


def test_local_mode_skips_domain_and_dns(run):
    result = run("--local", domain="localhost")
    assert result.returncode == 0, result.stdout
    assert "FAIL" not in result.stdout


def test_stack_down_fails_naming_the_check(run):
    result = run(FAKE_SERVICES="postgres web")
    assert result.returncode != 0
    assert "FAIL 5 not running: worker caddy" in result.stdout


def test_no_dns_fails(run):
    result = run(FAKE_NO_DNS="1")
    assert result.returncode != 0 and "FAIL 2" in result.stdout


def test_bad_certificate_fails(run):
    result = run(FAKE_CURL_FAIL="1")
    assert result.returncode != 0 and "FAIL 3" in result.stdout


def test_server_error_fails(run):
    result = run(FAKE_HTTP="502")
    assert result.returncode != 0 and "FAIL 3" in result.stdout


def test_missing_login_form_fails(run):
    result = run(FAKE_PAGE="Welcome")
    assert result.returncode != 0 and "FAIL 4" in result.stdout


def test_pending_migrations_fail(run):
    result = run(FAKE_MIGRATE_RC="1")
    assert result.returncode != 0 and "FAIL 6" in result.stdout


@pytest.mark.parametrize("counts", ["0 0", "2 0", "0 5"])
def test_empty_database_fails(run, counts):
    result = run(FAKE_COUNTS=counts)
    assert result.returncode != 0 and "FAIL 7" in result.stdout


def test_never_prints_secrets_and_never_writes(run):
    result = run()
    out = result.stdout + result.stderr
    assert "fake-secret-key-xyz" not in out
    assert "fake-pg-password-xyz" not in out
    calls = run.log.read_text()
    for forbidden in (
        "migrate --noinput",
        " up ",
        " down ",
        "create_user",
        "import_legacy",
    ):
        assert forbidden not in calls
