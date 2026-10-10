"""The audit log entry point. Placeholder: #39 replaces the body with a model write."""

import logging

logger = logging.getLogger("scheduling.audit")

QUICK_ACTION = "QUICK_ACTION"


def record_audit(action, user, week, params):
    """Record that `user` did `action` on the week starting `week` (a date)."""
    logger.info(
        "audit %s user=%s week=%s params=%s",
        action,
        getattr(user, "pk", None),
        week,
        params,
    )
