import os
import subprocess
import sys
from pathlib import Path

import pytest
from django.core.exceptions import ImproperlyConfigured

from config.env import (
    get_bool,
    get_domain,
    get_hosts,
    get_positive_int,
    get_required,
    parse_database_url,
)


def test_missing_required_names_variable():
    with pytest.raises(ImproperlyConfigured, match="SECRET_KEY"):
        get_required({}, "SECRET_KEY")


def test_empty_required_fails():
    with pytest.raises(ImproperlyConfigured, match="DATABASE_URL"):
        get_required({"DATABASE_URL": "  "}, "DATABASE_URL")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("true", True), ("TRUE", True), ("1", True), ("false", False), ("0", False)],
)
def test_bool_accepts(raw, expected):
    assert get_bool({"DEBUG": raw}, "DEBUG") is expected


def test_bool_default_false():
    assert get_bool({}, "DEBUG") is False


def test_bad_bool_fails():
    with pytest.raises(ImproperlyConfigured, match="DEBUG"):
        get_bool({"DEBUG": "maybe"}, "DEBUG")


def test_hosts_parsing():
    env = {"ALLOWED_HOSTS": "a.example.com, b.example.com,, "}
    assert get_hosts(env, "ALLOWED_HOSTS", required=True) == [
        "a.example.com",
        "b.example.com",
    ]


@pytest.mark.parametrize("env", [{}, {"ALLOWED_HOSTS": " , "}])
def test_hosts_required_when_not_debug(env):
    with pytest.raises(ImproperlyConfigured, match="ALLOWED_HOSTS"):
        get_hosts(env, "ALLOWED_HOSTS", required=True)


def test_hosts_optional_when_debug():
    assert get_hosts({}, "ALLOWED_HOSTS", required=False) == []


def test_timeout_default():
    assert get_positive_int({}, "SOLVER_TIMEOUT_SECONDS", 60) == 60


def test_timeout_value():
    assert (
        get_positive_int({"SOLVER_TIMEOUT_SECONDS": "30"}, "SOLVER_TIMEOUT_SECONDS", 60)
        == 30
    )


@pytest.mark.parametrize("raw", ["0", "-5", "abc", "1.5"])
def test_bad_timeout_fails(raw):
    with pytest.raises(ImproperlyConfigured, match="SOLVER_TIMEOUT_SECONDS"):
        get_positive_int({"SOLVER_TIMEOUT_SECONDS": raw}, "SOLVER_TIMEOUT_SECONDS", 60)


def test_database_url_parsing():
    cfg = parse_database_url("postgres://user:p%40ss@db.example.com:5433/name")
    assert cfg["ENGINE"] == "django.db.backends.postgresql"
    assert cfg["NAME"] == "name"
    assert cfg["USER"] == "user"
    assert cfg["PASSWORD"] == "p@ss"
    assert cfg["HOST"] == "db.example.com"
    assert cfg["PORT"] == "5433"


def test_non_postgres_url_fails():
    with pytest.raises(ImproperlyConfigured, match="DATABASE_URL"):
        parse_database_url("sqlite:///db.sqlite3")


def _check(env_overrides, drop=()):
    env = {k: v for k, v in os.environ.items() if k not in drop}
    env.update(
        SECRET_KEY="x",
        DEBUG="false",
        ALLOWED_HOSTS="localhost",
        DOMAIN="school.test",
        DATABASE_URL="postgres://u:p@localhost:5432/d",
        DJANGO_SETTINGS_MODULE="config.settings",
    )
    env.update(env_overrides)
    for k in drop:
        env.pop(k, None)
    return subprocess.run(
        [sys.executable, "manage.py", "check"], env=env, capture_output=True, text=True
    )


def test_startup_ok():
    assert _check({}).returncode == 0


@pytest.mark.parametrize("name", ["SECRET_KEY", "DATABASE_URL"])
def test_startup_fails_without_required(name):
    result = _check({}, drop=(name,))
    assert result.returncode != 0
    assert name in result.stderr


def test_startup_fails_without_hosts_unless_debug():
    result = _check({}, drop=("ALLOWED_HOSTS",))
    assert result.returncode != 0
    assert "ALLOWED_HOSTS" in result.stderr
    assert _check({"DEBUG": "true"}, drop=("ALLOWED_HOSTS",)).returncode == 0


def test_domain_required_in_production():
    result = _check({}, drop=("DOMAIN",))
    assert result.returncode != 0
    assert "DOMAIN" in result.stderr
    assert _check({"DEBUG": "true"}, drop=("DOMAIN",)).returncode == 0


@pytest.mark.parametrize("raw", ["https://a.test", "a.test/x", "a b"])
def test_domain_rejects_non_host(raw):
    with pytest.raises(ImproperlyConfigured, match="DOMAIN"):
        get_domain({"DOMAIN": raw}, required=True)


def test_domain_normalised():
    assert get_domain({"DOMAIN": " Example.COM "}, required=True) == "example.com"


def test_proxy_settings_in_production(settings):
    assert settings.SECURE_PROXY_SSL_HEADER == ("HTTP_X_FORWARDED_PROTO", "https")
    assert settings.SESSION_COOKIE_SECURE is True
    assert settings.CSRF_COOKIE_SECURE is True
    assert "https://school.test" in settings.CSRF_TRUSTED_ORIGINS
    assert settings.STATIC_ROOT.name == "staticfiles"


def test_env_example_lists_new_variables():
    text = (Path(__file__).resolve().parent.parent / ".env.example").read_text()
    assert "\nDOMAIN=" in text


def test_trusted_proxy_count_documented():
    root = Path(__file__).resolve().parent.parent
    assert "\nTRUSTED_PROXY_COUNT=0" in (root / ".env.example").read_text()
    assert (
        "\nTRUSTED_PROXY_COUNT=1"
        in (root / "deploy/env.production.example").read_text()
    )


def test_trusted_proxy_count_default_in_tests(settings):
    assert settings.TRUSTED_PROXY_COUNT == 0


@pytest.mark.parametrize(
    ("name", "default"),
    [("FEED_LOOKUP_MAX_FAILURES", 30), ("FEED_LOOKUP_WINDOW_SECONDS", 300)],
)
def test_feed_lookup_settings(name, default):
    assert get_positive_int({}, name, default) == default
    assert get_positive_int({name: " "}, name, default) == default
    assert get_positive_int({name: "7"}, name, default) == 7
    for bad in ("0", "-1", "abc"):
        with pytest.raises(ImproperlyConfigured, match=name):
            get_positive_int({name: bad}, name, default)


def test_feed_lookup_settings_wired_and_documented():
    from config import env

    assert env.DEFAULT_FEED_LOOKUP_MAX_FAILURES == 30
    assert env.DEFAULT_FEED_LOOKUP_WINDOW_SECONDS == 300
    result = _check({"FEED_LOOKUP_MAX_FAILURES": "0"})
    assert result.returncode != 0
    assert "FEED_LOOKUP_MAX_FAILURES" in result.stderr
    root = Path(__file__).resolve().parent.parent
    for rel in (".env.example", "deploy/env.production.example"):
        text = (root / rel).read_text()
        for name, default in (
            ("FEED_LOOKUP_MAX_FAILURES", 30),
            ("FEED_LOOKUP_WINDOW_SECONDS", 300),
        ):
            assert (
                f"# Optional, default {default}."
                in text.split(f"\n{name}=")[0][-400:].rsplit("\n\n", 1)[-1]
            )
            assert f"\n{name}={default}\n" in text
