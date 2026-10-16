import json
from io import StringIO

import pytest
from django.core.management import call_command

from scheduling.benchmark import generate_context, is_success, run_benchmark

# Loose on purpose: half the 60 s production timeout. CI is a shared machine and
# the 1 GB VM is slower than a dev machine, so this only catches a real blow-up.
NORMAL_CASE_MAX_SECONDS = 30

KEYS = {
    "classes",
    "teachers",
    "assignments",
    "pairings",
    "solve_seconds",
    "status",
    "quality_score",
    "quality_level",
    "violated_penalties",
    "peak_memory_mb",
}


def test_same_seed_same_context_different_seed_differs():
    assert generate_context(seed=3) == generate_context(seed=3)
    assert generate_context(seed=3) != generate_context(seed=4)


def test_default_instance_has_school_size():
    ctx = generate_context()
    assert len({a.class_id for a in ctx.assignments}) == 40
    assert 55 <= len({a.teacher_id for a in ctx.assignments}) <= 65
    assert ctx.pairings and ctx.stage_days and ctx.targets
    contractors = [t for t, kind in ctx.teacher_types.items() if kind == "CONTRATTO"]
    assert len(contractors) >= 15
    assert all(len(ctx.availability[t]) < 30 for t in contractors)


def test_small_instance_reports_all_keys():
    report = run_benchmark(n_classes=4)
    assert set(report) == KEYS
    assert report["classes"] == 4
    assert is_success(report), report["status"]


def test_command_json_prints_only_one_object():
    out = StringIO()
    call_command("benchmark_solver", "--classes", "4", "--json", stdout=out)
    assert set(json.loads(out.getvalue())) == KEYS


def test_command_prints_key_value_lines():
    out = StringIO()
    call_command("benchmark_solver", "--classes", "4", stdout=out)
    lines = dict(line.split(": ", 1) for line in out.getvalue().splitlines())
    assert set(lines) == KEYS


def test_command_exits_1_when_not_solved(monkeypatch):
    monkeypatch.setattr(
        "scheduling.management.commands.benchmark_solver.run_benchmark",
        lambda *a: {"status": "INFEASIBLE"},
    )
    out = StringIO()
    with pytest.raises(SystemExit) as exc:
        call_command("benchmark_solver", stdout=out)
    assert exc.value.code == 1
    assert "INFEASIBLE" in out.getvalue()


@pytest.mark.benchmark
def test_normal_case_solves_within_threshold():
    report = run_benchmark()
    assert is_success(report) and report["solve_seconds"] < NORMAL_CASE_MAX_SECONDS, (
        f"status {report['status']}, solved in {report['solve_seconds']} s "
        f"(limit {NORMAL_CASE_MAX_SECONDS} s)"
    )
