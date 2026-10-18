import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from django.test import Client

from accounts.models import User
from config.backup_status import read_backup_status

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 10, 17, 12, 0, 0, tzinfo=UTC)
DUMP = "gestione-docenti-20261017-023012-1-2.dump"


def marker(tmp_path, hours_ago, content=None):
    path = tmp_path / "last-success"
    when = NOW - timedelta(hours=hours_ago)
    path.write_text(
        content if content is not None else f"{when:%Y-%m-%dT%H:%M:%SZ} {DUMP}\n"
    )
    return path


def test_reader_valid_fresh(tmp_path):
    r = read_backup_status(marker(tmp_path, 5), 30, NOW)
    assert r == {"ok": True, "hours": 5, "stale": False, "dump": DUMP}


def test_reader_boundary(tmp_path):
    assert read_backup_status(marker(tmp_path, 30), 30, NOW)["stale"] is False
    assert read_backup_status(marker(tmp_path, 31), 30, NOW)["stale"] is True


@pytest.mark.parametrize("content", ["", "garbage\n", "2026-13-45T99:00:00Z x.dump\n"])
def test_reader_invalid(tmp_path, content):
    assert read_backup_status(marker(tmp_path, 1, content), 30, NOW) == {"ok": False}


def test_reader_missing_unset_future_unreadable(tmp_path):
    assert read_backup_status(tmp_path / "nope", 30, NOW) == {"ok": False}
    assert read_backup_status("", 30, NOW) == {"ok": False}
    assert read_backup_status(marker(tmp_path, -2), 30, NOW) == {"ok": False}
    assert read_backup_status(tmp_path, 30, NOW) == {"ok": False}  # a directory
    bad = tmp_path / "bin"
    bad.write_bytes(b"\xff\xfe\x00")
    assert read_backup_status(bad, 30, NOW) == {"ok": False}


def fresh_marker(tmp_path, hours_ago):
    when = datetime.now(UTC) - timedelta(hours=hours_ago, minutes=1)
    path = tmp_path / "last-success"
    path.write_text(f"{when:%Y-%m-%dT%H:%M:%SZ} {DUMP}\n")
    return path


def client_for(role):
    c = Client()
    if role:
        c.force_login(
            User.objects.create_user(f"{role}@example.com", "pw-long-enough", role)
        )
    return c


@pytest.mark.django_db
def test_admin_sees_fresh(tmp_path, settings):
    settings.BACKUP_MARKER_FILE = str(fresh_marker(tmp_path, 5))
    r = client_for("ADMIN").get("/")
    assert r.status_code == 200
    assert "Ultimo backup: 5 ore fa" in r.content.decode()
    assert "Backup non aggiornato" not in r.content.decode()


@pytest.mark.django_db
def test_admin_sees_stale(tmp_path, settings):
    settings.BACKUP_MARKER_FILE = str(fresh_marker(tmp_path, 52))
    html = client_for("ADMIN").get("/").content.decode()
    assert "Backup non aggiornato" in html
    assert "Ultimo backup: 52 ore fa" in html


@pytest.mark.django_db
@pytest.mark.parametrize("value", ["missing", ""])
def test_admin_sees_unverifiable(tmp_path, settings, value):
    settings.BACKUP_MARKER_FILE = str(tmp_path / "x") if value else ""
    r = client_for("ADMIN").get("/")
    assert r.status_code == 200
    assert "Backup non verificabile" in r.content.decode()


@pytest.mark.django_db
def test_segreteria_and_anonymous_see_nothing(tmp_path, settings):
    path = fresh_marker(tmp_path, 5)
    settings.BACKUP_MARKER_FILE = str(path)
    seg = client_for("SEGRETERIA").get("/")
    anon = client_for(None).get("/")
    assert seg.status_code == 200
    assert anon.status_code == 302
    for html in (seg.content.decode(), anon.content.decode()):
        for text in ("Ultimo backup", "Backup non", DUMP, str(path)):
            assert text not in html


def test_compose_mounts_marker_in_web_only():
    text = (ROOT / "docker-compose.yml").read_text()
    blocks = re.split(r"\n(?=  \w[\w-]*:\n)", text)
    with_marker = [b.split(":")[0].strip() for b in blocks if "BACKUP_MARKER_FILE" in b]
    with_mount = [b.split(":")[0].strip() for b in blocks if "/backups:ro" in b]
    assert with_marker == ["web"] and with_mount == ["web"]


def test_env_examples_document_settings():
    env = (ROOT / ".env.example").read_text()
    prod = (ROOT / "deploy" / "env.production.example").read_text()
    assert "# BACKUP_STALE_HOURS=" in env and "# BACKUP_MARKER_FILE=" in env
    assert "# BACKUP_STALE_HOURS=" in prod


# --- off-site line (#85) ---------------------------------------------------

REMOTE = "secret-remote:private-bucket/path"


def offsite_setup(tmp_path, settings, hours_ago=None, content=None, remote=True):
    """BACKUP_MARKER_FILE in tmp_path, a fresh last-success, an off-site marker."""
    settings.BACKUP_MARKER_FILE = str(fresh_marker(tmp_path, 1))
    settings.OFFSITE_CONFIGURED = remote
    marker = tmp_path / "last-offsite-success"
    if content is not None:
        marker.write_text(content)
    elif hours_ago is not None:
        when = datetime.now(UTC) - timedelta(hours=hours_ago, minutes=1)
        marker.write_text(f"{when:%Y-%m-%dT%H:%M:%SZ} {DUMP}\n")
    return marker


@pytest.mark.django_db
def test_admin_sees_offsite_fresh(tmp_path, settings):
    offsite_setup(tmp_path, settings, 70)
    html = client_for("ADMIN").get("/").content.decode()
    assert "Ultima copia off-site: 70 ore fa" in html
    assert "Copia off-site non aggiornata" not in html
    assert "Ultimo backup: 1 ore fa" in html


@pytest.mark.django_db
def test_admin_sees_offsite_stale_as_warning(tmp_path, settings):
    offsite_setup(tmp_path, settings, 200)
    html = client_for("ADMIN").get("/").content.decode()
    assert re.search(
        r'<p class="message message-warning"[^>]*>Ultima copia off-site: 200 ore fa'
        r".*Copia off-site non aggiornata</p>",
        html,
    )


@pytest.mark.django_db
@pytest.mark.parametrize(
    "case", ["absent", "garbage", "future", "two-lines", "marker-unset"]
)
def test_admin_sees_offsite_unverifiable(tmp_path, settings, case):
    content = {"garbage": "garbage\n", "two-lines": f"2026-01-01T00:00:00Z {DUMP}\nx\n"}
    marker = offsite_setup(tmp_path, settings, content=content.get(case))
    if case == "future":
        offsite_setup(tmp_path, settings, -5)
    if case == "marker-unset":
        offsite_setup(tmp_path, settings, 5)
        settings.BACKUP_MARKER_FILE = ""
    assert marker.parent == tmp_path
    html = client_for("ADMIN").get("/").content.decode()
    assert "Copia off-site non verificabile" in html
    assert "Ultima copia off-site" not in html


@pytest.mark.django_db
def test_offsite_threshold_comes_from_settings(tmp_path, settings):
    offsite_setup(tmp_path, settings, 30)
    settings.OFFSITE_STALE_HOURS = 24
    assert (
        "Copia off-site non aggiornata" in client_for("ADMIN").get("/").content.decode()
    )


@pytest.mark.django_db
def test_no_offsite_text_when_remote_not_configured(tmp_path, settings):
    offsite_setup(tmp_path, settings, 70, remote=False)
    admin = client_for("ADMIN")
    html = admin.get("/").content.decode()
    assert "off-site" not in html.lower()
    assert "Ultimo backup: 1 ore fa" in html
    (tmp_path / "last-offsite-success").unlink()  # marker-less, still silent
    assert "off-site" not in admin.get("/").content.decode().lower()


@pytest.mark.django_db
def test_segreteria_and_anonymous_see_no_offsite(tmp_path, settings):
    marker = offsite_setup(tmp_path, settings, 70)
    settings.OFFSITE_RCLONE_REMOTE = REMOTE
    seg = client_for("SEGRETERIA").get("/")
    anon = client_for(None).get("/")
    assert seg.status_code == 200 and anon.status_code == 302
    for html in (seg.content.decode(), anon.content.decode()):
        for text in ("off-site", DUMP, str(marker), REMOTE, "private-bucket"):
            assert text not in html


@pytest.mark.django_db
def test_admin_html_never_holds_the_remote(tmp_path, settings):
    offsite_setup(tmp_path, settings, 70)
    settings.OFFSITE_RCLONE_REMOTE = REMOTE
    html = client_for("ADMIN").get("/").content.decode()
    for text in (REMOTE, "private-bucket", DUMP, str(tmp_path)):
        assert text not in html


def test_env_examples_document_offsite_threshold():
    for name in (".env.example", "deploy/env.production.example"):
        assert "# OFFSITE_STALE_HOURS=192" in (ROOT / name).read_text()
