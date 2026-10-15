"""Logging filter that keeps magic-link tokens out of log records."""

import logging
import re

# Path of the teacher page (availability/urls.py): /l/<token>/
_TOKEN_PATH = re.compile(r"(/l/)([A-Za-z0-9_-]{0,6})[^\s\"'?]*")


# Path of a calendar feed (feeds/urls.py): /feeds/<token>.ics
_FEED_PATH = re.compile(r"(/feeds/)([A-Za-z0-9_-]{0,6})[A-Za-z0-9_-]*\.ics")


def redact(text):
    text = _FEED_PATH.sub(r"\1\2....ics", text)
    return _TOKEN_PATH.sub(r"\1\2...", text)


class RedactLinkTokens(logging.Filter):
    def filter(self, record):
        try:
            message = record.getMessage()
        except Exception:
            return True
        redacted = redact(message)
        if redacted != message:
            record.msg, record.args = redacted, ()
        return True
