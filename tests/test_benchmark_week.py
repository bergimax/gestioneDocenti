import dataclasses
import datetime
import json
import subprocess
import sys
from io import StringIO

import pytest
from django.conf import settings
from django.core.management import CommandError, call_command

from scheduling import benchmark
from school.models import Assignment, Class, School, Subject, Teacher
from tests.test_solver_benchmark import KEYS

MON = "2026-01-05"
SECRET = "ZZQUUXNAME"


@pytest.fixture
def school(db):
    return School.objects.create(
        name=f"Scuola{SECRET}",
        formative_year="2025-2026",
        year_start=datetime.date(2025, 9, 1),
        year_end=datetime.date(2026, 6, 30),
    )


@pytest.fixture
def week(school):
    subject = Subject.objects.create(
        school=school, name=f"Mat{SECRET}", kind="TEORIA", cognitive_weight="MEDIO"
    )
    klass = Class.objects.create(school=school, name=f"Classe{SECRET}")
    teacher = Teacher.objects.create(
        school=school, name=f"Doc{SECRET}", type="ASSUNTO", first_time=False
    )
    Assignment.objects.create(
        school=school,
        teacher=teacher,
        school_class=klass,
        subject=subject,
        ore_totali=4,
    )
    return school


@pytest.fixture
def run(capsys):
    def _run(*args):
        out = StringIO()
        code = 0
        try:
            call_command("benchmark_solver", *args, stdout=out)
        except SystemExit as exc:
            code = exc.code
        return code, out.getvalue(), capsys.readouterr().err

    return _run


def test_week_prints_the_twelve_keys_and_database_counts(week, run):
    code, out, _ = run("--week", MON)
    lines = dict(line.split(": ", 1) for line in out.splitlines())
    assert code == 0 and set(lines) == KEYS
    assert [lines[k] for k in ("classes", "teachers", "assignments", "pairings")] == [
        "1",
        "1",
        "1",
        "0",
    ]


def test_week_json_has_the_same_keys(week, run):
    code, out, _ = run("--week", MON, "--json")
    assert code == 0 and set(json.loads(out)) == KEYS


def test_week_solves_with_the_configured_timeout(week, monkeypatch, settings, run):
    settings.SOLVER_TIMEOUT_SECONDS = 17
    seen = []
    real = benchmark.solve
    monkeypatch.setattr(
        benchmark, "solve", lambda ctx: (seen.append(ctx.timeout_seconds), real(ctx))[1]
    )
    run("--week", MON)
    assert seen == [17.0]


def test_week_infeasible_exits_1_and_still_prints_report(week, monkeypatch, run):
    real = benchmark.solve

    def fake(ctx):
        return dataclasses.replace(real(ctx), status="INFEASIBLE")

    monkeypatch.setattr(benchmark, "solve", fake)
    code, out, _ = run("--week", MON)
    assert code == 1 and "status: INFEASIBLE" in out


@pytest.mark.parametrize("value", ["2026-01-06", "not-a-date"])
def test_week_bad_date_fails_cleanly(week, value, run):
    code, out, err = run("--week", value)
    assert code == 1 and out == ""
    assert err.startswith("benchmark failed: ") and "Traceback" not in err


def test_week_without_assignments_fails(school, run):
    code, out, err = run("--week", MON)
    assert code == 1 and out == ""
    assert "benchmark failed: nessuna assegnazione per la settimana" in err


def test_week_without_school_fails(db, run):
    code, out, err = run("--week", MON)
    assert code == 1 and out == ""
    assert err.startswith("benchmark failed: ") and "Traceback" not in err


@pytest.mark.parametrize("extra", [["--classes", "4"], ["--seed", "1"]])
def test_week_with_classes_or_seed_is_an_error(week, extra, run):
    with pytest.raises(CommandError, match="--week.*--classes.*--seed"):
        call_command("benchmark_solver", "--week", MON, *extra)


def test_week_output_has_no_names_or_ids(week, run):
    _, out, err = run("--week", MON)
    assert SECRET not in out + err
    for line in out.splitlines():
        key, value = line.split(": ", 1)
        if key not in ("status", "quality_level"):
            assert value.replace(".", "").replace("-", "").isdigit()


def test_generated_run_touches_no_database(run):
    # No `db` fixture: any query would raise, be caught and turn into exit 1.
    code, out, _ = run("--classes", "4")
    assert code == 0 and {x.split(": ")[0] for x in out.splitlines()} == KEYS


def test_generated_run_defaults_unchanged(monkeypatch, run):
    calls = []
    monkeypatch.setattr(
        "scheduling.management.commands.benchmark_solver.run_benchmark",
        lambda *a: calls.append(a) or {"status": "OPTIMAL"},
    )
    code, out, _ = run()
    assert code == 0 and out == "status: OPTIMAL\n"
    assert calls == [(40, 0, float(settings.SOLVER_TIMEOUT_SECONDS))]


def test_benchmark_module_imports_no_django_model():
    code = (
        "import sys; import scheduling.benchmark; "
        "assert 'school.models' not in sys.modules; "
        "assert 'django.db' not in sys.modules"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
