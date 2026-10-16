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
