"""Test environment: fixed non-secret values, set before config.settings loads.

A DATABASE_URL exported in the shell overrides the default local PostgreSQL.
"""

import os

os.environ["SECRET_KEY"] = "test-only-not-a-secret"
os.environ["DEBUG"] = "false"
os.environ["DOMAIN"] = "school.test"
os.environ["ALLOWED_HOSTS"] = "localhost,testserver"
os.environ["SOLVER_TIMEOUT_SECONDS"] = "60"
os.environ["SOLVER_EXPLAIN_BUDGET_SECONDS"] = "10"
os.environ["SOLVER_STALE_MARGIN_SECONDS"] = "120"
os.environ["AVAILABILITY_LINK_TTL_HOURS"] = "72"
os.environ["LINK_LOOKUP_MAX_FAILURES"] = "10"
os.environ["LINK_LOOKUP_WINDOW_SECONDS"] = "300"
os.environ["LOGIN_MAX_FAILURES"] = "5"
os.environ["LOGIN_WINDOW_SECONDS"] = "900"
os.environ["LOGIN_ADDRESS_MAX_FAILURES"] = "20"
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ["ANTHROPIC_MODEL"] = "claude-haiku-4-5"
os.environ["ANTHROPIC_TIMEOUT_SECONDS"] = "30"
os.environ.setdefault(
    "DATABASE_URL", "postgres://postgres:postgres@localhost:5432/gestione_docenti"
)

from config.settings import *  # noqa: E402, F403

# Enqueueing in tests records nothing and runs nothing; tests call the task.
TASKS = {"default": {"BACKEND": "django_tasks.backends.dummy.DummyBackend"}}
