from io import StringIO

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

# Every purge run by the daily cron line (deploy/purge.sh). Each name is a
# management command that accepts --dry-run and needs no other argument
# (its retention comes from its own env setting). To schedule a new purge,
# add its name here.
SCHEDULED_PURGES = ("purge_parked_uploads", "purge_availability_links")


class Command(BaseCommand):
    help = (
        "Run every registered purge in order (SCHEDULED_PURGES). --dry-run is "
        "passed to each. One failing purge does not stop the others; the "
        "command exits non-zero after all have run. Exit code 0 also when "
        "nothing is stale."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="report what would be deleted, delete nothing",
        )

    def handle(self, *args, dry_run, **options):
        ok = failed = 0
        for name in SCHEDULED_PURGES:
            out = StringIO()
            try:
                call_command(name, *(["--dry-run"] if dry_run else []), stdout=out)
            except Exception as exc:  # one purge must not stop the others
                failed += 1
                self.stderr.write(f"[{name}] FAILED: {exc}")
                continue
            ok += 1
            self.stdout.write(f"[{name}] {out.getvalue().strip()}")
        self.stdout.write(f"purges: {ok} ok, {failed} failed")
        if failed:
            raise CommandError(f"{failed} purge(s) failed.")
