from django_tasks_db.management.commands.db_worker import Command as DbWorker


class Command(DbWorker):
    """Start the background worker that runs timetable generation jobs.

    Takes the options of `db_worker` (for example `--batch` to process the
    pending jobs and exit, `--max-tasks 1` to stop after one job). Jobs run one
    at a time per process. Ctrl-C (SIGINT) lets a running solve finish, then
    exits cleanly; a second Ctrl-C stops at once.
    """

    help = "Run the worker for timetable generation jobs (one solve at a time)"
