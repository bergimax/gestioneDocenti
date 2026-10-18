"""deploy/backup.sh, backup-offsite.sh and restore.sh, with a fake docker and rclone.

The fake `docker` answers the pg_dump / pg_restore / psql calls the scripts make
through the postgres container, so these tests need no database. The real
dump and restore are exercised by deploy/test-restore.sh.
"""

import os
import subprocess
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"

FAKE_DOCKER = """#!/usr/bin/env bash
args="$*"
case "$args" in
  *pg_dump*)
    [ -z "${FAKE_DUMP_FAIL:-}" ] || { echo "pg_dump: connection refused" >&2; exit 1; }
    printf 'PGDMP-fake-dump'; exit 0 ;;
  *pg_restore*--list*|*pg_restore*-l*)
    head -c 5 | grep -q PGDMP ;;
  *psql*)
    echo "${FAKE_TABLES:-0}" ;;
  *pg_restore*)
    cat > "$FAKE_RESTORE_OUT" ;;
esac
"""

FAKE_RCLONE = """#!/usr/bin/env bash
[ -z "${FAKE_RCLONE_FAIL:-}" ] || { echo "rclone: boom" >&2; exit 1; }
echo "$@" > "$FAKE_RCLONE_OUT"
echo "${RCLONE_CONFIG_X_TYPE:-}" >> "$FAKE_RCLONE_OUT"
"""


def script(path, body):
    path.write_text(body)
    path.chmod(0o755)


@pytest.fixture
def env(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script(bin_dir / "docker", FAKE_DOCKER)
    backups = tmp_path / "backups"
    base = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "ENV_FILE": str(tmp_path / "missing.env"),
        "BACKUP_DIR": str(backups),
        "POSTGRES_PASSWORD": "s3cret-fake-password",
        "FAKE_RESTORE_OUT": str(tmp_path / "restore.out"),
        "FAKE_RCLONE_OUT": str(tmp_path / "rclone.out"),
    }
    base["bin"] = bin_dir
    base["backups"] = backups
    return base


def run(env, name, *args, **extra):
    e = {k: str(v) for k, v in env.items() if k not in ("bin", "backups")}
    e.update(extra)
    return subprocess.run(
        [str(DEPLOY / name), *args], env=e, capture_output=True, text=True
    )


def dumps(env):
    return sorted(p.name for p in env["backups"].glob("gestione-docenti-*.dump"))


def test_scripts_are_executable_and_strict():
    for name in ("backup.sh", "backup-offsite.sh", "restore.sh", "test-restore.sh"):
        path = DEPLOY / name
        assert os.access(path, os.X_OK), name
        assert "set -euo pipefail" in path.read_text()
        assert subprocess.run(["bash", "-n", str(path)]).returncode == 0


def test_backup_creates_verified_nonempty_dump(env):
    r = run(env, "backup.sh")
    assert r.returncode == 0, r.stderr
    (name,) = dumps(env)
    assert (env["backups"] / name).stat().st_size > 0
    assert not list(env["backups"].glob("*.partial"))
    assert "s3cret-fake-password" not in r.stdout + r.stderr


def test_retention_keeps_newest_n_and_foreign_files(env):
    env["backups"].mkdir()
    for i in range(1, 7):
        (env["backups"] / f"gestione-docenti-20200101-00000{i}-1.dump").write_text("x")
    (env["backups"] / "notes.txt").write_text("keep")
    (env["backups"] / "gestione-docenti-20200101-000009-1.dump.partial").write_text("p")
    (env["backups"] / "other-20200101-000001-1.dump").write_text("o")
    r = run(env, "backup.sh", BACKUP_KEEP="3")
    assert r.returncode == 0, r.stderr
    names = dumps(env)
    assert len(names) == 3
    assert not any("20200101-000001" in n or "20200101-000002" in n for n in names)
    for f in ("notes.txt", "gestione-docenti-20200101-000009-1.dump.partial"):
        assert (env["backups"] / f).exists()
    assert (env["backups"] / "other-20200101-000001-1.dump").exists()


@pytest.mark.parametrize("keep", ["0", "-1", "abc", "1.5"])
def test_invalid_keep_exits_nonzero_and_deletes_nothing(env, keep):
    env["backups"].mkdir()
    (env["backups"] / "gestione-docenti-20200101-000001-1.dump").write_text("x")
    r = run(env, "backup.sh", BACKUP_KEEP=keep)
    assert r.returncode != 0
    assert "BACKUP_KEEP" in r.stderr
    assert dumps(env) == ["gestione-docenti-20200101-000001-1.dump"]


def test_keep_one_never_removes_the_new_dump_and_runs_do_not_collide(env):
    a = run(env, "backup.sh", BACKUP_KEEP="5")
    b = run(env, "backup.sh", BACKUP_KEEP="5")
    assert a.returncode == b.returncode == 0
    assert len(dumps(env)) == 2
    c = run(env, "backup.sh", BACKUP_KEEP="1")
    assert c.returncode == 0
    (left,) = dumps(env)
    assert left in c.stdout


def test_failing_dump_leaves_nothing_and_prunes_nothing(env):
    env["backups"].mkdir()
    for i in range(1, 4):
        (env["backups"] / f"gestione-docenti-20200101-00000{i}-1.dump").write_text("x")
    r = run(env, "backup.sh", BACKUP_KEEP="1", FAKE_DUMP_FAIL="1")
    assert r.returncode != 0
    assert "pg_dump" in r.stderr
    assert len(dumps(env)) == 3
    assert not list(env["backups"].glob("*.partial"))


def test_unwritable_backup_dir_fails_clearly(env):
    env["backups"].mkdir()
    env["backups"].chmod(0o500)
    try:
        if os.access(env["backups"], os.W_OK):
            pytest.skip("running as a user that ignores directory permissions")
        r = run(env, "backup.sh")
    finally:
        env["backups"].chmod(0o700)
    assert r.returncode != 0
    assert "backup directory" in r.stderr


def test_offsite_unset_prints_one_line_and_copies_nothing(env):
    script(env["bin"] / "rclone", FAKE_RCLONE)
    r = run(env, "backup-offsite.sh")
    assert r.returncode == 0
    assert r.stdout.strip() == "off-site not configured, skipped"
    assert not Path(env["FAKE_RCLONE_OUT"]).exists()


def test_offsite_copies_newest_dump(env):
    script(env["bin"] / "rclone", FAKE_RCLONE)
    env["backups"].mkdir()
    (env["backups"] / "gestione-docenti-20200101-000001-1.dump").write_text("old")
    (env["backups"] / "gestione-docenti-20200102-000001-1.dump").write_text("new")
    r = run(
        env,
        "backup-offsite.sh",
        OFFSITE_RCLONE_REMOTE="local:/dest/",
        RCLONE_CONFIG_X_TYPE="s3",
    )
    assert r.returncode == 0, r.stderr
    out = Path(env["FAKE_RCLONE_OUT"]).read_text()
    assert "20200102-000001-1.dump local:/dest/gestione-docenti-20200102" in out
    assert "20200101" not in out


def test_offsite_reads_rclone_credentials_from_env_file(env, tmp_path):
    script(env["bin"] / "rclone", FAKE_RCLONE)
    env["backups"].mkdir()
    (env["backups"] / "gestione-docenti-20200101-000001-1.dump").write_text("d")
    envfile = tmp_path / "prod.env"
    envfile.write_text("OFFSITE_RCLONE_REMOTE=r:b\nRCLONE_CONFIG_X_TYPE=s3\n")
    r = run(env, "backup-offsite.sh", ENV_FILE=str(envfile))
    assert r.returncode == 0, r.stderr
    assert Path(env["FAKE_RCLONE_OUT"]).read_text().splitlines()[-1] == "s3"
    assert "s3" not in r.stdout + r.stderr


def test_offsite_failures_exit_nonzero(env):
    env["backups"].mkdir()
    (env["backups"] / "gestione-docenti-20200101-000001-1.dump").write_text("d")
    # rclone missing: a PATH with the fake docker only, and no rclone anywhere
    r = run(env, "backup-offsite.sh", OFFSITE_RCLONE_REMOTE="r:b", PATH="/usr/bin:/bin")
    if not Path("/usr/bin/rclone").exists():
        assert r.returncode != 0
        assert "rclone" in r.stderr
    script(env["bin"] / "rclone", FAKE_RCLONE)
    r = run(env, "backup-offsite.sh", OFFSITE_RCLONE_REMOTE="r:b", FAKE_RCLONE_FAIL="1")
    assert r.returncode != 0
    assert "FAILED" in r.stderr


def test_offsite_without_dump_fails(env):
    script(env["bin"] / "rclone", FAKE_RCLONE)
    r = run(env, "backup-offsite.sh", OFFSITE_RCLONE_REMOTE="r:b")
    assert r.returncode != 0
    assert "no dump" in r.stderr


def test_restore_refuses_bad_dump_files(env, tmp_path):
    notdump = tmp_path / "x.dump"
    notdump.write_text("hello")
    unreadable = tmp_path / "u.dump"
    unreadable.write_text("PGDMP")
    unreadable.chmod(0)
    cases = [str(tmp_path / "missing.dump"), str(tmp_path), str(notdump)]
    if not os.access(unreadable, os.R_OK):
        cases.append(str(unreadable))
    for case in cases:
        r = run(env, "restore.sh", case)
        assert r.returncode != 0, case
        assert "restore FAILED" in r.stderr
    assert not Path(env["FAKE_RESTORE_OUT"]).exists()


def test_restore_refuses_nonempty_database_without_flag(env, tmp_path):
    dump = tmp_path / "d.dump"
    dump.write_text("PGDMP-fake")
    r = run(env, "restore.sh", str(dump), FAKE_TABLES="12")
    assert r.returncode != 0
    assert "--yes-overwrite" in r.stderr
    assert not Path(env["FAKE_RESTORE_OUT"]).exists()
    ok = run(env, "restore.sh", str(dump), "--yes-overwrite", FAKE_TABLES="12")
    assert ok.returncode == 0, ok.stderr
    assert Path(env["FAKE_RESTORE_OUT"]).read_text() == "PGDMP-fake"


def test_restore_into_empty_database(env, tmp_path):
    dump = tmp_path / "d.dump"
    dump.write_text("PGDMP-fake")
    r = run(env, "restore.sh", str(dump))
    assert r.returncode == 0, r.stderr


MARKER_RE = r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ gestione-docenti-.*\.dump$"


def test_backup_writes_marker(env):
    import re

    r = run(env, "backup.sh")
    assert r.returncode == 0, r.stderr
    text = (env["backups"] / "last-success").read_text()
    assert text.count("\n") == 1
    assert re.match(MARKER_RE, text.rstrip("\n"))
    assert (env["backups"] / text.split()[1]).exists()
    assert not list(env["backups"].glob("*last-success.tmp*"))


@pytest.mark.parametrize(
    "extra", [{"FAKE_DUMP_FAIL": "1"}, {"BACKUP_KEEP": "abc"}], ids=["dump", "keep"]
)
@pytest.mark.parametrize("existed", [False, True])
def test_failed_backup_leaves_marker(env, extra, existed):
    marker = env["backups"] / "last-success"
    env["backups"].mkdir()
    if existed:
        marker.write_text("2026-01-01T00:00:00Z gestione-docenti-1-2-3.dump\n")
    before = marker.read_bytes() if existed else None
    assert run(env, "backup.sh", **extra).returncode != 0
    assert (marker.read_bytes() if marker.exists() else None) == before


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permissions")
def test_unwritable_dir_leaves_marker(env):
    marker = env["backups"] / "last-success"
    env["backups"].mkdir()
    marker.write_text("2026-01-01T00:00:00Z gestione-docenti-1-2-3.dump\n")
    env["backups"].chmod(0o555)
    try:
        assert run(env, "backup.sh").returncode != 0
        assert marker.read_text().startswith("2026-01-01")
    finally:
        env["backups"].chmod(0o755)


def test_retention_keeps_marker(env):
    for _ in range(3):
        assert run(env, "backup.sh", BACKUP_KEEP="1").returncode == 0
    (env["backups"] / "notes.txt").write_text("x")
    assert run(env, "backup.sh", BACKUP_KEEP="1").returncode == 0
    assert len(dumps(env)) == 1
    assert (env["backups"] / "last-success").exists()
    assert (env["backups"] / "notes.txt").exists()
    listed = subprocess.run(
        [
            "bash",
            "-c",
            f'source {DEPLOY}/lib.sh; list_dumps "{env["backups"]}"',
        ],
        env={**os.environ, "ENV_FILE": "/nonexistent"},
        capture_output=True,
        text=True,
    ).stdout
    assert "last-success" not in listed


def write_marker(env, hours_ago, name="gestione-docenti-20261017-023012-1-2.dump"):
    from datetime import UTC, datetime, timedelta

    env["backups"].mkdir(exist_ok=True)
    when = datetime.now(UTC) - timedelta(hours=hours_ago)
    (env["backups"] / "last-success").write_text(f"{when:%Y-%m-%dT%H:%M:%SZ} {name}\n")


def test_status_fresh(env):
    write_marker(env, 5)
    r = run(env, "backup-status.sh")
    assert r.returncode == 0
    assert "last backup: 5 hours ago (gestione-docenti-" in r.stdout
    assert "s3cret" not in r.stdout + r.stderr


def test_status_under_one_hour(env):
    write_marker(env, 0.2)
    r = run(env, "backup-status.sh")
    assert r.returncode == 0
    assert "less than 1 hour ago" in r.stdout


def test_status_stale_default(env):
    write_marker(env, 52)
    r = run(env, "backup-status.sh")
    assert r.returncode == 1
    assert "52 hours ago, STALE (threshold 30 h)" in r.stdout


@pytest.mark.parametrize("hours,code", [(30, 0), (31, 1)])
def test_status_threshold_boundary(env, hours, code):
    write_marker(env, hours)
    assert run(env, "backup-status.sh", BACKUP_STALE_HOURS="30").returncode == code
    write_marker(env, 40)
    assert run(env, "backup-status.sh", BACKUP_STALE_HOURS="48").returncode == 0


@pytest.mark.parametrize("content", [None, "", "garbage\n", "FUTURE", "two\nlines\n"])
def test_status_invalid_marker(env, content):
    env["backups"].mkdir()
    marker = env["backups"] / "last-success"
    if content == "FUTURE":
        write_marker(env, -3)
    elif content is not None:
        marker.write_text(content)
    r = run(env, "backup-status.sh")
    assert r.returncode == 2
    assert f"no valid backup marker in {env['backups']}" in r.stderr
    assert "hours ago" not in r.stdout


def test_status_missing_dir(env):
    r = run(env, "backup-status.sh")
    assert r.returncode == 2
    assert str(env["backups"]) in r.stderr


@pytest.mark.parametrize("value", ["0", "-5", "abc"])
def test_status_bad_threshold(env, value):
    write_marker(env, 1)
    r = run(env, "backup-status.sh", BACKUP_STALE_HOURS=value)
    assert r.returncode == 2
    assert "BACKUP_STALE_HOURS" in r.stderr


def test_status_script_is_strict_and_cron_runs_it():
    path = DEPLOY / "backup-status.sh"
    assert os.access(path, os.X_OK)
    assert "set -euo pipefail" in path.read_text()
    assert subprocess.run(["bash", "-n", str(path)]).returncode == 0
    assert "deploy/backup-status.sh" in (DEPLOY / "cron.example").read_text()


# --- off-site marker and status line (#85) ---------------------------------

OFFSITE_RE = r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ gestione-docenti-.*\.dump$"
OLD_OFFSITE = "2026-01-01T00:00:00Z gestione-docenti-1-2-3.dump\n"
REMOTE = "secret-remote:private-bucket/path"


def offsite_marker(env):
    return env["backups"] / "last-offsite-success"


def two_dumps(env):
    env["backups"].mkdir(exist_ok=True)
    (env["backups"] / "gestione-docenti-20200101-000001-1.dump").write_text("old")
    (env["backups"] / "gestione-docenti-20200102-000001-1.dump").write_text("new")


def test_offsite_success_writes_marker(env):
    import re
    from datetime import UTC, datetime

    script(env["bin"] / "rclone", FAKE_RCLONE)
    two_dumps(env)
    r = run(env, "backup-offsite.sh", OFFSITE_RCLONE_REMOTE=REMOTE)
    assert r.returncode == 0, r.stderr
    text = offsite_marker(env).read_text()
    assert text.count("\n") == 1
    assert re.match(OFFSITE_RE, text.rstrip("\n"))
    stamp, name = text.split()
    assert name == "gestione-docenti-20200102-000001-1.dump"
    written = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    assert abs((datetime.now(UTC) - written).total_seconds()) < 60
    assert not list(env["backups"].glob("*last-offsite-success.tmp*"))
    assert not list(env["backups"].glob(".*tmp*"))


def offsite_failure_cases(env):
    """Yield (id, extra env, setup) for every run that must not touch the marker."""
    return {
        "unset": {},
        "no-rclone": {"OFFSITE_RCLONE_REMOTE": "r:b", "PATH": "/usr/bin:/bin"},
        "rclone-fails": {"OFFSITE_RCLONE_REMOTE": "r:b", "FAKE_RCLONE_FAIL": "1"},
        "no-dump": {"OFFSITE_RCLONE_REMOTE": "r:b"},
    }


@pytest.mark.parametrize("case", ["unset", "no-rclone", "rclone-fails", "no-dump"])
@pytest.mark.parametrize("existed", [False, True])
def test_offsite_marker_untouched_when_nothing_was_copied(env, case, existed):
    if case == "no-rclone" and Path("/usr/bin/rclone").exists():
        pytest.skip("a real rclone is installed")
    if case != "no-rclone":
        script(env["bin"] / "rclone", FAKE_RCLONE)
    env["backups"].mkdir()
    if case != "no-dump":
        two_dumps(env)
    if existed:
        offsite_marker(env).write_text(OLD_OFFSITE)
    r = run(env, "backup-offsite.sh", **offsite_failure_cases(env)[case])
    if case == "unset":
        assert r.returncode == 0
        assert r.stdout.strip() == "off-site not configured, skipped"
    else:
        assert r.returncode != 0
    if case == "rclone-fails":
        assert "FAILED" in r.stderr
    if existed:
        assert offsite_marker(env).read_text() == OLD_OFFSITE
    else:
        assert not offsite_marker(env).exists()
    assert not list(env["backups"].glob(".*tmp*"))


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permissions")
def test_offsite_marker_not_writable_fails_after_copy(env):
    # rclone itself makes the directory read-only, i.e. after the dump is placed
    script(
        env["bin"] / "rclone",
        f'#!/usr/bin/env bash\nchmod 555 "{env["backups"]}"\n',
    )
    two_dumps(env)
    try:
        r = run(env, "backup-offsite.sh", OFFSITE_RCLONE_REMOTE=REMOTE)
    finally:
        env["backups"].chmod(0o755)
    assert r.returncode != 0
    assert "marker" in r.stderr and "could not be written" in r.stderr
    assert not offsite_marker(env).exists()
    assert not list(env["backups"].glob(".*tmp*"))


def test_offsite_marker_path_is_a_directory_fails(env):
    script(env["bin"] / "rclone", FAKE_RCLONE)
    two_dumps(env)
    offsite_marker(env).mkdir()
    r = run(env, "backup-offsite.sh", OFFSITE_RCLONE_REMOTE=REMOTE)
    assert r.returncode != 0
    assert "could not be written" in r.stderr
    assert list(offsite_marker(env).iterdir()) == []
    assert not list(env["backups"].glob(".*tmp*"))


def test_retention_keeps_offsite_marker(env):
    env["backups"].mkdir()
    offsite_marker(env).write_text(OLD_OFFSITE)
    for _ in range(3):
        assert run(env, "backup.sh", BACKUP_KEEP="1").returncode == 0
    assert len(dumps(env)) == 1
    assert offsite_marker(env).read_text() == OLD_OFFSITE
    listed = subprocess.run(
        ["bash", "-c", f'source {DEPLOY}/lib.sh; list_dumps "{env["backups"]}"'],
        env={**os.environ, "ENV_FILE": "/nonexistent"},
        capture_output=True,
        text=True,
    ).stdout
    assert "last-offsite-success" not in listed


def write_offsite(env, hours_ago, content=None):
    from datetime import UTC, datetime, timedelta

    env["backups"].mkdir(exist_ok=True)
    when = datetime.now(UTC) - timedelta(hours=hours_ago, minutes=1)
    name = "gestione-docenti-20261017-023012-1-2.dump"
    offsite_marker(env).write_text(
        content if content is not None else f"{when:%Y-%m-%dT%H:%M:%SZ} {name}\n"
    )


def status(env, **extra):
    extra.setdefault("OFFSITE_RCLONE_REMOTE", REMOTE)
    r = run(env, "backup-status.sh", **extra)
    for secret in ("s3cret", REMOTE, "private-bucket"):
        assert secret not in r.stdout + r.stderr
    return r


def test_status_without_remote_is_unchanged(env):
    write_marker(env, 5)
    write_offsite(env, 500)  # ignored: not configured
    r = run(env, "backup-status.sh")
    assert r.returncode == 0
    assert r.stdout.count("\n") == 1 and "off-site" not in r.stdout + r.stderr
    write_marker(env, 52)
    r = run(env, "backup-status.sh")
    assert r.returncode == 1 and "off-site" not in r.stdout
    (env["backups"] / "last-success").write_text("garbage\n")
    r = run(env, "backup-status.sh")
    assert r.returncode == 2 and "off-site" not in r.stdout + r.stderr


def test_status_offsite_fresh(env):
    write_marker(env, 5)
    write_offsite(env, 70)
    r = status(env)
    assert r.returncode == 0
    lines = r.stdout.splitlines()
    assert lines[0].startswith("last backup: 5 hours ago")
    assert lines[1].startswith("last off-site copy: 70 hours ago (gestione-docenti-")


@pytest.mark.parametrize("hours,code", [(192, 0), (193, 1)])
def test_status_offsite_default_boundary(env, hours, code):
    write_marker(env, 5)
    write_offsite(env, hours)
    r = status(env)
    assert r.returncode == code
    if code:
        assert (
            f"last off-site copy: {hours} hours ago, STALE (threshold 192 h)"
            in r.stdout
        )


def test_status_offsite_custom_threshold_and_under_one_hour(env):
    write_marker(env, 5)
    write_offsite(env, 30)
    r = status(env, OFFSITE_STALE_HOURS="24")
    assert r.returncode == 1
    assert "STALE (threshold 24 h)" in r.stdout
    write_offsite(env, 0, "")  # replaced below with a minutes-old marker
    from datetime import UTC, datetime, timedelta

    when = datetime.now(UTC) - timedelta(minutes=5)
    offsite_marker(env).write_text(
        f"{when:%Y-%m-%dT%H:%M:%SZ} gestione-docenti-20261017-023012-1-2.dump\n"
    )
    r = status(env)
    assert r.returncode == 0 and "last off-site copy: less than 1 hour ago" in r.stdout


@pytest.mark.parametrize(
    "content", [None, "", "garbage\n", "FUTURE", "two\nlines\n"], ids=repr
)
def test_status_offsite_invalid_marker(env, content):
    write_marker(env, 5)
    if content == "FUTURE":
        write_offsite(env, -3)
    elif content is not None:
        write_offsite(env, 0, content)
    r = status(env)
    assert r.returncode == 2
    assert f"no valid off-site marker in {env['backups']}" in r.stderr
    assert "last off-site copy" not in r.stdout
    assert "last backup: 5 hours ago" in r.stdout


def test_status_combined_exit_codes(env):
    write_marker(env, 52)  # stale local
    write_offsite(env, 5)
    r = status(env)
    assert r.returncode == 1
    assert "STALE" in r.stdout and "last off-site copy: 5 hours ago" in r.stdout
    write_marker(env, 5)  # fresh local, invalid off-site
    write_offsite(env, 0, "garbage\n")
    assert status(env).returncode == 2
    write_marker(env, 52)  # stale local, invalid off-site: worst is 2
    assert status(env).returncode == 2
    (env["backups"] / "last-success").write_text("garbage\n")  # invalid local
    write_offsite(env, 5)
    r = status(env)
    assert r.returncode == 2
    assert "no valid backup marker" in r.stderr
    assert "last off-site copy: 5 hours ago" in r.stdout


@pytest.mark.parametrize("value", ["0", "-5", "abc"])
@pytest.mark.parametrize("remote", [REMOTE, ""])
def test_status_bad_offsite_threshold(env, value, remote):
    write_marker(env, 1)
    write_offsite(env, 1)
    r = status(env, OFFSITE_STALE_HOURS=value, OFFSITE_RCLONE_REMOTE=remote)
    assert r.returncode == 2
    assert "OFFSITE_STALE_HOURS" in r.stderr


@pytest.mark.parametrize("value", ["0", "-5", "abc"])
def test_django_rejects_bad_offsite_threshold(value):
    root = DEPLOY.parent
    r = subprocess.run(
        [
            "python",
            "-c",
            "import django.conf as c; c.settings.OFFSITE_STALE_HOURS",
        ],
        cwd=root,
        env={
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "config.settings_test",
            "OFFSITE_STALE_HOURS": value,
        },
        capture_output=True,
        text=True,
    )
    assert r.returncode != 0
    assert "OFFSITE_STALE_HOURS must be a positive integer" in r.stderr


def test_docs_mention_offsite_line():
    assert "off-site" in (DEPLOY / "cron.example").read_text()
    readme = (DEPLOY / "README.md").read_text()
    for text in ("last-offsite-success", "OFFSITE_STALE_HOURS", "no valid off-site"):
        assert text in readme
    for name in ("backup-offsite.sh", "backup-status.sh"):
        assert "set -euo pipefail" in (DEPLOY / name).read_text()
        assert subprocess.run(["bash", "-n", str(DEPLOY / name)]).returncode == 0
