import os

from django.core.management.base import BaseCommand, CommandError

from school import legacy_import

ENV_VAR = "IMPORT_SOURCE_URL"


class Command(BaseCommand):
    help = (
        "Import the data of the old FastAPI system into this database. "
        "The old PostgreSQL database is read through --source URL, or the "
        f"{ENV_VAR} environment variable when --source is not given; it is "
        "opened read-only and the URL is never printed. The whole import is "
        "one transaction and the target must be empty (users excepted). "
        "With --dry-run the report is printed and nothing is written. "
        "Exit codes: 0 = imported (or dry run) and verified; 1 = refused "
        "(missing source, non-empty target), aborted by an unexpected error, "
        "or the verification found a mismatch; in every case of 1 nothing "
        "is written. Imported users have no password: run "
        "manage.py changepassword for each."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--source",
            help=f"PostgreSQL URL of the old database (default: ${ENV_VAR})",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="print the report of what would be created, write nothing",
        )

    def handle(self, *args, source, dry_run, **options):
        url = source or os.environ.get(ENV_VAR, "")
        if not url:
            raise CommandError(f"No source: pass --source or set {ENV_VAR}.")
        try:
            report, ok = legacy_import.run(url, dry_run=dry_run)
        except legacy_import.ImportRefused as exc:
            raise CommandError(str(exc)) from None
        self.stdout.write(report)
        if not ok:
            raise CommandError("Verification failed: nothing was written.")
