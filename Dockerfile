FROM python:3.13-slim

# uv is a build tool, copied from the official image; not a project dependency.
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv

# WeasyPrint 70: Pango (and HarfBuzz through it), fontconfig and fonts.
# Cairo is not required by WeasyPrint 70 but is harmless.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b libcairo2 \
        fontconfig fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=0 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .

# Non-root user; the static volume is created here so it is owned by it.
RUN useradd --uid 1000 --create-home app \
    && mkdir -p /app/staticfiles \
    && chown app:app /app/staticfiles
USER app

# DEBUG is deliberately not set: it defaults to false.
EXPOSE 8000
CMD ["gunicorn", "-c", "deploy/gunicorn.conf.py", "config.wsgi:application"]
