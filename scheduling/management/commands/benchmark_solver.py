import json
import sys

from django.conf import settings
from django.core.management.base import BaseCommand

from scheduling.benchmark import (
    DEFAULT_CLASSES,
    DEFAULT_SEED,
    is_success,
    run_benchmark,
)


class Command(BaseCommand):
    help = (
        "Generate a deterministic school-sized week, solve it and print the "
        "numbers (size, solve time, status, quality, violated penalties, peak "
        "memory). Needs no database. Exit code 0 when the status is OPTIMAL or "
        "FEASIBLE, 1 otherwise (INFEASIBLE, UNKNOWN or an error)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--seed",
            type=int,
            default=DEFAULT_SEED,
            help=f"seed of the generated week (default {DEFAULT_SEED})",
        )
        parser.add_argument(
            "--classes",
            type=int,
            default=DEFAULT_CLASSES,
            help=f"number of classes (default {DEFAULT_CLASSES})",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            help="print one JSON object instead of key: value lines",
        )

    def handle(self, *args, **options):
        try:
            report = run_benchmark(
                options["classes"],
                options["seed"],
                float(settings.SOLVER_TIMEOUT_SECONDS),
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
