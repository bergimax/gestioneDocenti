from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from availability import links


def positive_int(text):
    try:
        value = int(text)
    except ValueError:
        value = 0
    if value < 1:
        raise CommandError("--days must be a positive integer.")
    return value


class Command(BaseCommand):
    help = (
        "Delete availability links expired or revoked for longer than "
        "AVAILABILITY_LINK_PURGE_DAYS (default 30) days, or --days, and the "
        "lookup failures outside the rate-limit window. Active links are never "
        "removed. With --dry-run nothing is deleted. Exit code 0 also when "
        "nothing is stale."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            help="override AVAILABILITY_LINK_PURGE_DAYS (positive integer)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="report what would be deleted, delete nothing",
        )

    def handle(self, *args, days, dry_run, **options):
        if days is None:
            days = settings.AVAILABILITY_LINK_PURGE_DAYS
        else:
            days = positive_int(days)
        n_links, n_failures = links.purge_dead_links(days, dry_run=dry_run)
        verb = "Would delete" if dry_run else "Deleted"
        self.stdout.write(
            f"{verb} {n_links} availability link(s) and {n_failures} "
            f"lookup failure(s) older than {days} days."
        )
