from django_tasks_db.management.commands.db_worker import Command as DbWorker

from scheduling.jobs import fail_stale_jobs


class Command(DbWorker):
    """Start the background worker that runs timetable generation jobs.

    Takes the options of `db_worker` (for example `--batch` to process the
    pending jobs and exit, `--max-tasks 1` to stop after one job). Jobs run one
    at a time per process. Ctrl-C (SIGINT) lets a running solve finish, then
    exits cleanly; a second Ctrl-C stops at once.
    """

    help = "Run the worker for timetable generation jobs (one solve at a time)"

    def handle(self, *args, **options):
        from config.telemetry import setup_telemetry

        setup_telemetry("worker")
        # jobs a previous, killed worker left RUNNING can never finish
        fail_stale_jobs()
        return super().handle(*args, **options)
