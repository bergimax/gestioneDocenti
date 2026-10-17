"""gunicorn settings for the web container (Dockerfile CMD and compose agree)."""

bind = "0.0.0.0:8000"
workers = 2

# No access log, on purpose: request lines carry bearer tokens in the path
# (/l/<token>/, /feeds/<token>.ics). Caddy keeps the (redacted) access log.
accesslog = None
