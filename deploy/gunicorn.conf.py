"""gunicorn settings for the web container (Dockerfile CMD and compose agree)."""

bind = "0.0.0.0:8000"
workers = 2

# No access log, on purpose: request lines carry bearer tokens in the path
# (/l/<token>/, /feeds/<token>.ics). Caddy keeps the (redacted) access log.
accesslog = None


def post_fork(server, worker):
    """Start telemetry in each worker: exporter threads do not survive a fork."""
    import os

    from config.telemetry import setup_telemetry

    # the app (and wsgi.py) is imported after this hook; the instrumentor reads settings
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    setup_telemetry("web")
