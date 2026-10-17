import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured

from accounts.models import LoginFailure
from availability.models import LinkLookupFailure
from config.env import get_non_negative_int
from tests.test_settings import _check

User = get_user_model()
BAD_TOKEN = "A" * 43


def login_failure(client, **extra):
    client.post(
        "/login/",
        {"username": "a@x.it", "password": "wrong"},
        REMOTE_ADDR="10.0.0.1",
        **extra,
    )
    return set(LoginFailure.objects.values_list("client_address", flat=True))


def lookup_failure(client, **extra):
    client.get(f"/l/{BAD_TOKEN}/", REMOTE_ADDR="10.0.0.1", **extra)
    return set(LinkLookupFailure.objects.values_list("client_address", flat=True))


@pytest.mark.django_db
def test_login_and_lookup_counted_for_forwarded_address(client, settings):
    settings.TRUSTED_PROXY_COUNT = 1
    assert login_failure(client, HTTP_X_FORWARDED_FOR="203.0.113.9") == {"203.0.113.9"}
    assert lookup_failure(client, HTTP_X_FORWARDED_FOR="203.0.113.9") == {"203.0.113.9"}


@pytest.mark.django_db
def test_rightmost_entry_wins(client, settings):
    settings.TRUSTED_PROXY_COUNT = 1
    got = login_failure(client, HTTP_X_FORWARDED_FOR="198.51.100.1, 203.0.113.9")
    assert got == {"203.0.113.9"}


@pytest.mark.django_db
def test_count_two_takes_second_from_right(client, settings):
    settings.TRUSTED_PROXY_COUNT = 2
    got = login_failure(
        client, HTTP_X_FORWARDED_FOR="198.51.100.1, 203.0.113.9, 192.0.2.7"
    )
    assert got == {"203.0.113.9"}


@pytest.mark.django_db
def test_default_zero_ignores_header(client, settings):
    assert settings.TRUSTED_PROXY_COUNT == 0
    assert login_failure(client, HTTP_X_FORWARDED_FOR="203.0.113.9") == {"10.0.0.1"}


@pytest.mark.django_db
@pytest.mark.parametrize("header", [None, "", "not-an-ip", " , ", "1.2.3.4, not-an-ip"])
def test_unusable_header_falls_back(client, settings, header):
    settings.TRUSTED_PROXY_COUNT = 1
    extra = {} if header is None else {"HTTP_X_FORWARDED_FOR": header}
    assert login_failure(client, **extra) == {"10.0.0.1"}


@pytest.mark.django_db
def test_too_few_entries_falls_back(client, settings):
    settings.TRUSTED_PROXY_COUNT = 2
    assert login_failure(client, HTTP_X_FORWARDED_FOR="203.0.113.9") == {"10.0.0.1"}


@pytest.mark.django_db
def test_ipv6_accepted(client, settings):
    settings.TRUSTED_PROXY_COUNT = 1
    assert login_failure(client, HTTP_X_FORWARDED_FOR="2001:db8::1") == {"2001:db8::1"}


def test_setting_parsing():
    assert get_non_negative_int({}, "X", 0) == 0
    assert get_non_negative_int({"X": "0"}, "X", 3) == 0
    assert get_non_negative_int({"X": "2"}, "X", 0) == 2
    for raw in ("-1", "abc", "1.5"):
        with pytest.raises(ImproperlyConfigured, match="X"):
            get_non_negative_int({"X": raw}, "X", 0)


@pytest.mark.parametrize("value", ["-1", "abc"])
def test_bad_trusted_proxy_count_fails_startup(value):
    result = _check({"TRUSTED_PROXY_COUNT": value})
    assert result.returncode != 0
    assert "TRUSTED_PROXY_COUNT" in result.stderr


def test_zero_allowed_at_startup():
    assert _check({"TRUSTED_PROXY_COUNT": "0"}).returncode == 0
