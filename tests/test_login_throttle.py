import datetime

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from accounts.models import LoginFailure

User = get_user_model()
PW = "correct-horse"
GENERIC = "Email o password non corretti."
LOCKED = "Troppi tentativi di accesso. Riprova tra qualche minuto."


@pytest.fixture
def user(db):
    return User.objects.create_user("a@x.it", PW, "SEGRETERIA")


def post(client, email, password="wrong", addr="10.0.0.1", **extra):
    return client.post(
        "/login/",
        {"username": email, "password": password},
        REMOTE_ADDR=addr,
        **extra,
    )


def fail(client, n, email="a@x.it", addr="10.0.0.1"):
    for _ in range(n):
        assert post(client, email, addr=addr).status_code == 200


def backdate(seconds=901):
    LoginFailure.objects.update(
        attempted_at=timezone.now() - datetime.timedelta(seconds=seconds)
    )


def test_sixth_attempt_locked_even_with_correct_password(client, user):
    for _ in range(5):
        r = post(client, "a@x.it")
        assert r.status_code == 200
        assert GENERIC in r.content.decode()
    r = post(client, "a@x.it", PW)
    assert r.status_code == 429
    assert LOCKED in r.content.decode()
    assert GENERIC not in r.content.decode()
    assert r["Retry-After"] == "900"
    assert "_auth_user_id" not in client.session


def test_unknown_and_inactive_count_and_lock(client, user):
    User.objects.create_user("off@x.it", PW, "SEGRETERIA")
    User.objects.filter(email="off@x.it").update(is_active=False)
    for email in ("nobody@x.it", "off@x.it"):
        fail(client, 5, email=email)
        assert post(client, email, PW).status_code == 429


def test_email_case_and_spaces_share_counter(client, user):
    for email in ("A@x.it ", " a@X.IT", "a@x.it", "A@X.it", "a@x.it "):
        post(client, email)
    assert LoginFailure.objects.filter(email="a@x.it").count() == 5
    assert post(client, "a@x.it", PW).status_code == 429


def test_window_expiry_allows_login(client, user):
    fail(client, 5)
    assert post(client, "a@x.it", PW).status_code == 429
    backdate()
    assert post(client, "a@x.it", PW).status_code == 302


def test_address_lock_across_emails(client, user):
    for i in range(20):
        post(client, f"u{i}@x.it")
    assert post(client, "fresh@x.it").status_code == 429
    assert post(client, "a@x.it", PW, addr="10.0.0.2").status_code == 302


def test_account_failures_do_not_block_other_account_elsewhere(client, user):
    User.objects.create_user("b@x.it", PW, "SEGRETERIA")
    fail(client, 5)
    assert post(client, "b@x.it", PW, addr="10.0.0.2").status_code == 302


def test_refused_attempts_not_recorded(client, user):
    fail(client, 5)
    for _ in range(3):
        assert post(client, "a@x.it").status_code == 429
    assert LoginFailure.objects.count() == 5


def test_success_clears_account_failures_only(client, user):
    fail(client, 3)
    post(client, "other@x.it", addr="10.0.0.1")
    assert post(client, "a@x.it", PW).status_code == 302
    assert not LoginFailure.objects.filter(email="a@x.it").exists()
    assert LoginFailure.objects.filter(email="other@x.it").exists()


def test_recording_deletes_old_rows(client, user):
    fail(client, 2)
    backdate()
    post(client, "a@x.it")
    assert LoginFailure.objects.count() == 1


def test_forwarded_for_ignored(client, user):
    for i in range(20):
        post(client, f"u{i}@x.it", HTTP_X_FORWARDED_FOR=f"1.1.1.{i}")
    assert set(LoginFailure.objects.values_list("client_address", flat=True)) == {
        "10.0.0.1"
    }
    assert post(client, "x@x.it", HTTP_X_FORWARDED_FOR="9.9.9.9").status_code == 429


def test_locked_get_renders_and_logged_in_unaffected(client, user):
    fail(client, 5)
    assert client.get("/login/").status_code == 200
    client.force_login(user)
    assert client.get("/login/").status_code == 302
    r = post(client, "a@x.it", PW)
    assert r.status_code == 302


def test_max_failures_honoured_from_settings(client, user, settings):
    settings.LOGIN_MAX_FAILURES = 2
    fail(client, 2)
    assert post(client, "a@x.it", PW).status_code == 429


@pytest.mark.parametrize(
    "name", ["LOGIN_MAX_FAILURES", "LOGIN_WINDOW_SECONDS", "LOGIN_ADDRESS_MAX_FAILURES"]
)
def test_env_values_validated(name):
    from django.core.exceptions import ImproperlyConfigured

    from config.env import get_positive_int

    for bad in ("0", "-1", "x"):
        with pytest.raises(ImproperlyConfigured):
            get_positive_int({name: bad}, name, 5)
