import datetime
import json
import sys

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from scheduling.benchmark import (
    DEFAULT_CLASSES,
    DEFAULT_SEED,
    is_success,
    run_benchmark,
    run_benchmark_week,
)


class Command(BaseCommand):
    help = (
        "Generate a deterministic school-sized week, solve it and print the "
        "numbers (size, solve time, status, quality, violated penalties, peak "
        "memory). Needs no database, unless --week YYYY-MM-DD (a Monday) is "
        "given: then that real week of the configured database is solved. "
        "Exit code 0 when the status is OPTIMAL or FEASIBLE, 1 otherwise "
        "(INFEASIBLE, UNKNOWN or an error)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--seed",
            type=int,
            default=None,
            help=f"seed of the generated week (default {DEFAULT_SEED})",
        )
        parser.add_argument(
            "--classes",
            type=int,
            default=None,
            help=f"number of classes (default {DEFAULT_CLASSES})",
        )
        parser.add_argument(
            "--week",
            default=None,
            help="Monday (YYYY-MM-DD) of a real week of the database to solve; "
            "not combinable with --classes or --seed",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            help="print one JSON object instead of key: value lines",
        )

    def handle(self, *args, **options):
        week = options["week"]
        if week is not None and (
            options["classes"] is not None or options["seed"] is not None
        ):
            raise CommandError("--week cannot be combined with --classes or --seed")
        timeout = float(settings.SOLVER_TIMEOUT_SECONDS)
        try:
            if week is not None:
                report = run_benchmark_week(datetime.date.fromisoformat(week), timeout)
            else:
                classes = options["classes"]
                seed = options["seed"]
                report = run_benchmark(
                    DEFAULT_CLASSES if classes is None else classes,
                    DEFAULT_SEED if seed is None else seed,
                    timeout,
                )
        except Exception as exc:
            sys.stderr.write(f"benchmark failed: {exc}\n")
            sys.exit(1)
        if options["json"]:
            self.stdout.write(json.dumps(report))
        else:
            for key, value in report.items():
                self.stdout.write(f"{key}: {value}")
        if not is_success(report):
            sys.exit(1)
