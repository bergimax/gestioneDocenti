from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from school import setup_service


def positive_int(text):
    try:
        value = int(text)
    except ValueError:
        value = 0
    if value < 1:
        raise CommandError("--max-age-hours must be a positive integer.")
    return value


class Command(BaseCommand):
    help = (
        "Delete parked setup uploads not touched for longer than "
        "SETUP_UPLOAD_MAX_AGE_HOURS (default 72) hours, or --max-age-hours. "
        "Only ParkedUpload rows are removed. With --dry-run nothing is "
        "deleted. Exit code 0 also when nothing is stale."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--max-age-hours",
            help="override SETUP_UPLOAD_MAX_AGE_HOURS (positive integer)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="report what would be deleted, delete nothing",
        )

    def handle(self, *args, max_age_hours, dry_run, **options):
        if max_age_hours is None:
            hours = settings.SETUP_UPLOAD_MAX_AGE_HOURS
        else:
            hours = positive_int(max_age_hours)
        file_types = setup_service.purge_parked_uploads(hours, dry_run=dry_run)
        verb = "Would delete" if dry_run else "Deleted"
        detail = f" ({', '.join(file_types)})" if file_types else ""
        self.stdout.write(
            f"{verb} {len(file_types)} parked upload(s) older than "
            f"{hours} hours{detail}."
        )
