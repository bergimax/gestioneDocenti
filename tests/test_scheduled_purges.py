import datetime
import os
import re
import subprocess
from io import StringIO
from pathlib import Path
from unittest import mock

import pytest
from django.core.management import call_command, get_commands, load_command_class
from django.core.management.base import CommandError

from availability.models import AvailabilityLink
from school.management.commands import run_scheduled_purges
from school.management.commands.run_scheduled_purges import SCHEDULED_PURGES
from school.models import ParkedUpload, School, Teacher

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"
NOW = datetime.datetime.now(datetime.UTC)
DAY = datetime.timedelta(days=1)
N = [0]


@pytest.fixture
def teacher(db):
    school = School.objects.create(
        name="S",
        formative_year="2026-2027",
        year_start=datetime.date(2026, 9, 1),
        year_end=datetime.date(2027, 6, 30),
    )
    return Teacher.objects.create(
        school=school, name="ROSSI", type="CONTRATTO", first_time=False
    )


def upload(file_type, age_days):
    row = ParkedUpload.objects.create(file_type=file_type, source="csv", filename="f")
    ParkedUpload.objects.filter(pk=row.pk).update(updated_at=NOW - age_days * DAY)
    return row


def link(teacher, expired_days=None):
    N[0] += 1
    expires = NOW - expired_days * DAY if expired_days else NOW + 5 * DAY
    return AvailabilityLink.objects.create(
        teacher=teacher,
        week_start=datetime.date(2026, 9, 7) + datetime.timedelta(days=7 * N[0]),
        token_hash=f"{N[0]:064d}",
        expires_at=expires,
    )


def run(*args):
    out, err = StringIO(), StringIO()
    try:
        call_command("run_scheduled_purges", *args, stdout=out, stderr=err)
    except CommandError:
        return out.getvalue(), err.getvalue(), True
    return out.getvalue(), err.getvalue(), False


def stale_and_fresh(teacher):
    upload("calendar", 30)
    upload("teachers", 0)
    link(teacher, expired_days=400)
    link(teacher)


def test_deletes_old_keeps_fresh_in_order(teacher):
    stale_and_fresh(teacher)
    out, err, failed = run()
    assert not failed
    lines = out.strip().splitlines()
    assert lines[0].startswith("[purge_parked_uploads] Deleted 1")
    assert lines[1].startswith("[purge_availability_links] Deleted 1")
    assert lines[2] == "purges: 2 ok, 0 failed"
    assert list(ParkedUpload.objects.values_list("file_type", flat=True)) == [
        "teachers"
    ]
    assert AvailabilityLink.objects.count() == 1


def test_dry_run_deletes_nothing(teacher):
    stale_and_fresh(teacher)
    out, err, failed = run("--dry-run")
    assert not failed
    assert out.count("Would delete") == 2
    assert ParkedUpload.objects.count() == 2
    assert AvailabilityLink.objects.count() == 2


def test_nothing_stale_exits_zero_with_zero_counts(db):
    out, err, failed = run()
    assert not failed
    assert "Deleted 0 parked upload(s)" in out
    assert "Deleted 0 availability link(s)" in out
    assert out.strip().endswith("purges: 2 ok, 0 failed")


def test_failing_purge_does_not_stop_the_next(teacher):
    stale_and_fresh(teacher)
    with mock.patch(
        "school.management.commands.purge_parked_uploads.Command.handle",
        side_effect=RuntimeError("boom"),
    ):
        out, err, failed = run()
    assert failed
    assert "[purge_parked_uploads] FAILED: boom" in err
    assert "[purge_availability_links] Deleted 1" in out
    assert out.strip().endswith("purges: 1 ok, 1 failed")
    assert AvailabilityLink.objects.count() == 1


def test_registry_names_are_commands_with_dry_run():
    assert isinstance(run_scheduled_purges.SCHEDULED_PURGES, tuple)
    for name in SCHEDULED_PURGES:
        assert name in get_commands()
        app = get_commands()[name]
        command = load_command_class(app, name)
        assert "--dry-run" in command.create_parser("manage.py", name).format_help()


def test_cron_example_has_one_valid_daily_purge_line():
    text = (DEPLOY / "cron.example").read_text()
    lines = [x for x in text.splitlines() if x.strip() and not x.startswith("#")]
    purge = [x for x in lines if "deploy/purge.sh" in x]
    assert len(purge) == 1
    m = re.fullmatch(
        r"(\d+) (\d+) (\*) (\*) (\*) cd (\S+) && deploy/purge\.sh "
        r">> backups/cron\.log 2>&1",
        purge[0],
    )
    assert m
    minute, hour = int(m[1]), int(m[2])
    assert 0 <= minute < 60 and 0 <= hour < 24
    assert (hour, minute) not in {(2, 30), (3, 30), (8, 0)}
    assert len(lines) == 4
    for script in ("backup.sh", "backup-offsite.sh", "backup-status.sh"):
        assert sum(f"deploy/{script} " in x for x in lines) == 1
    assert "30 2 * * * cd /opt/gestione-docenti && deploy/backup.sh" in text
    assert "30 3 * * 0 cd /opt/gestione-docenti && deploy/backup-offsite.sh" in text
    assert "0 8 * * * cd /opt/gestione-docenti && deploy/backup-status.sh" in text


def test_purge_script_is_strict_and_passes_args_through():
    path = DEPLOY / "purge.sh"
    text = path.read_text()
    assert os.access(path, os.X_OK)
    assert text.startswith("#!/usr/bin/env bash")
    assert "set -euo pipefail" in text
    assert "lib.sh" in text
    assert "exec -T web" in text
    assert "run_scheduled_purges" in text
    assert '"$@"' in text
    assert subprocess.run(["bash", "-n", str(path)]).returncode == 0


def test_readme_documents_scheduled_purges():
    text = (DEPLOY / "README.md").read_text()
    assert "### Scheduled purges" in text
    section = text.split("### Scheduled purges")[1].split("\n### ")[0]
    for needle in ("04:15", "deploy/purge.sh", "--dry-run", "backups/cron.log"):
        assert needle in section
    assert "SCHEDULED_PURGES" in section
    assert "purges daily (04:15)" in text
