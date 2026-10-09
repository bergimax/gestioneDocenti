"""Test environment: fixed non-secret values, set before config.settings loads.

A DATABASE_URL exported in the shell overrides the default local PostgreSQL.
"""

import os

os.environ["SECRET_KEY"] = "test-only-not-a-secret"
os.environ["DEBUG"] = "false"
os.environ["ALLOWED_HOSTS"] = "localhost,testserver"
os.environ["SOLVER_TIMEOUT_SECONDS"] = "60"
os.environ.setdefault(
    "DATABASE_URL", "postgres://postgres:postgres@localhost:5432/gestione_docenti"
)

from config.settings import *  # noqa: E402, F403
