import re
from pathlib import Path

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.urls import reverse

from config.env import get_non_negative_int
from tests.test_settings import _check

ROOT = Path(__file__).resolve().parent.parent
CADDYFILE = (ROOT / "deploy" / "Caddyfile").read_text()

CSP = (
    "default-src 'self'; script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
    "style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; "
    "connect-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'"
)


def _block(text, header):
    """Body of the first `header {` block in text (brace matched), and what precedes."""
    m = re.search(rf"^\s*{header} \{{$", text, re.M)
    assert m, header
    depth, i = 1, m.end()
    while depth:
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        i += 1
    return text[m.end() : i - 1], text[: m.start()]


def _site_body():
    return _block(CADDYFILE, r"\{\$DOMAIN\}")[0]


def _header_lines():
    body, _ = _block(_site_body(), "header")
    return [ln.strip() for ln in body.splitlines() if ln.strip()]


def test_django_settings(settings):
    assert settings.SECURE_CONTENT_TYPE_NOSNIFF is True
    assert settings.SECURE_REFERRER_POLICY == "same-origin"
    assert settings.X_FRAME_OPTIONS == "DENY"
    assert settings.SECURE_HSTS_INCLUDE_SUBDOMAINS is False
    assert settings.SECURE_HSTS_PRELOAD is False
    assert not getattr(settings, "SECURE_SSL_REDIRECT", False)
    assert "django.middleware.clickjacking.XFrameOptionsMiddleware" in (
        settings.MIDDLEWARE
    )


def test_static_headers_once_each(client):
    response = client.get(reverse("login"))
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "same-origin"
    assert response.headers["X-Frame-Options"] == "DENY"
    names = [k.lower() for k, _ in response.items()]
    for name in ("x-content-type-options", "referrer-policy", "x-frame-options"):
        assert names.count(name) == 1


@pytest.mark.parametrize("forwarded", [{}, {"HTTP_X_FORWARDED_PROTO": "https"}])
def test_hsts_off_by_default(client, settings, forwarded):
    assert settings.SECURE_HSTS_SECONDS == 0
    response = client.get(reverse("login"), **forwarded)
    assert "Strict-Transport-Security" not in response.headers


def test_hsts_on_only_over_https(client, settings):
    settings.SECURE_HSTS_SECONDS = 300
    secure = client.get(reverse("login"), HTTP_X_FORWARDED_PROTO="https")
    assert secure.headers["Strict-Transport-Security"] == "max-age=300"
    assert "Strict-Transport-Security" not in client.get(reverse("login")).headers


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({}, 0),
        ({"SECURE_HSTS_SECONDS": ""}, 0),
        ({"SECURE_HSTS_SECONDS": "  "}, 0),
        ({"SECURE_HSTS_SECONDS": "300"}, 300),
    ],
)
def test_hsts_env_parsing(env, expected):
    assert get_non_negative_int(env, "SECURE_HSTS_SECONDS", 0) == expected


@pytest.mark.parametrize("raw", ["-1", "abc", "1.5"])
def test_hsts_env_rejects(raw):
    with pytest.raises(ImproperlyConfigured, match="SECURE_HSTS_SECONDS"):
        get_non_negative_int({"SECURE_HSTS_SECONDS": raw}, "SECURE_HSTS_SECONDS", 0)


def test_hsts_bad_value_fails_startup():
    result = _check({"SECURE_HSTS_SECONDS": "abc"})
    assert result.returncode != 0
    assert "SECURE_HSTS_SECONDS" in result.stderr


def test_hsts_variable_documented():
    pattern = r"((?:^#.*\n)+)SECURE_HSTS_SECONDS=\n"
    comments = []
    for path in (".env.example", "deploy/env.production.example"):
        m = re.search(pattern, (ROOT / path).read_text(), re.M)
        assert m, path
        assert "Optional" in m.group(1)
        assert "default off" in m.group(1)
        comments.append(m.group(1))
    for step in ("300", "86400", "2592000", "31536000"):
        assert step in comments[1]
    assert (
        'os.environ["SECURE_HSTS_SECONDS"] = "0"'
        in (ROOT / "config" / "settings_test.py").read_text()
    )


def test_caddy_header_block():
    lines = _header_lines()
    assert "X-Content-Type-Options nosniff" in lines
    assert "Referrer-Policy same-origin" in lines
    assert "X-Frame-Options DENY" in lines
    assert "-Server" in lines
    assert f'Content-Security-Policy-Report-Only "{CSP}"' in lines
    # Site level and before handle_path, so /static/* is covered.
    assert len(re.findall(r"^\s*header \{$", CADDYFILE, re.M)) == 1
    site = _site_body()
    _, before = _block(site, "header")
    assert before.count("{") == before.count("}")
    assert "handle" not in before.replace("log", "")
    assert site.index("header {") < site.index("handle_path")


def test_caddy_has_no_hsts_or_enforcing_csp():
    assert "Strict-Transport-Security" not in CADDYFILE
    assert not re.search(r"Content-Security-Policy(?!-Report-Only)", CADDYFILE)
    assert "report-uri" not in CADDYFILE
    assert "report-to" not in CADDYFILE


def test_caddy_and_django_agree(settings):
    lines = dict(ln.split(" ", 1) for ln in _header_lines() if " " in ln)
    assert settings.SECURE_CONTENT_TYPE_NOSNIFF
    assert lines["X-Content-Type-Options"] == "nosniff"
    assert lines["Referrer-Policy"] == settings.SECURE_REFERRER_POLICY
    assert lines["X-Frame-Options"] == settings.X_FRAME_OPTIONS


def test_readme_section():
    readme = (ROOT / "deploy" / "README.md").read_text()
    assert "## Security headers" in readme
    section = readme.split("## Security headers")[1].split("\n## ")[0]
    for needle in ("300", "86400", "2592000", "31536000", "preload", "#150"):
        assert needle in section
    assert "includeSubDomains" in section
