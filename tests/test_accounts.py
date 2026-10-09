from io import StringIO

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction
from django.test import Client

User = get_user_model()
PW = "correct-horse"


@pytest.fixture
def user(db):
    return User.objects.create_user("mario@example.com", PW, "SEGRETERIA")


def test_user_model_and_session_age():
    assert settings.AUTH_USER_MODEL == "accounts.User"
    assert settings.SESSION_COOKIE_AGE == 7 * 24 * 60 * 60


def test_email_unique_case_insensitive(user):
    with pytest.raises(IntegrityError), transaction.atomic():
        User.objects.create_user("MARIO@example.com", PW, "ADMIN")


@pytest.mark.parametrize("role", ["", "BOSS"])
def test_role_limited(db, role):
    with pytest.raises(IntegrityError), transaction.atomic():
        User.objects.create_user("x@example.com", PW, role)


def test_login_success(client, user):
    r = client.post("/login/", {"username": "mario@example.com", "password": PW})
    assert r.status_code == 302
    assert r.url == "/"
    assert "_auth_user_id" in client.session


def test_login_email_case_insensitive(client, user):
    r = client.post("/login/", {"username": "Mario@Example.com", "password": PW})
    assert r.status_code == 302
    assert "_auth_user_id" in client.session


def test_login_failures_share_one_message(client, user):
    bad_pw = client.post("/login/", {"username": user.email, "password": "nope"})
    unknown = client.post("/login/", {"username": "no@example.com", "password": PW})
    assert bad_pw.status_code == unknown.status_code == 200
    assert "Email o password non corretti." in bad_pw.content.decode()
    assert bad_pw.content.decode().count("non corretti") == 1
    assert "Email o password non corretti." in unknown.content.decode()
    assert "_auth_user_id" not in client.session


def test_inactive_user_cannot_login(client, user):
    user.is_active = False
    user.save()
    r = client.post("/login/", {"username": user.email, "password": PW})
    assert r.status_code == 200
    assert "Email o password non corretti." in r.content.decode()
    assert "_auth_user_id" not in client.session


def test_logout_post_only(client, user):
    client.force_login(user)
    assert client.get("/logout/").status_code == 405
    assert "_auth_user_id" in client.session
    r = client.post("/logout/")
    assert r.status_code == 302
    assert r.url == "/login/"
    assert "_auth_user_id" not in client.session


def test_logged_in_user_redirected_from_login(client, user):
    client.force_login(user)
    assert client.get("/login/").status_code == 302


def test_change_password_requires_login_and_returns(client, user):
    r = client.get("/password/")
    assert r.status_code == 302
    assert r.url == "/login/?next=/password/"
    r = client.post(r.url, {"username": user.email, "password": PW})
    assert r.url == "/password/"


def test_login_ignores_external_next(client, user):
    r = client.post(
        "/login/?next=https://evil.example/",
        {"username": user.email, "password": PW},
    )
    assert r.url == "/"


def change(client, old=PW, new="brand-new-pw", confirm=None):
    return client.post(
        "/password/",
        {
            "old_password": old,
            "new_password1": new,
            "new_password2": new if confirm is None else confirm,
        },
    )


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"old": "wrong"}, "La password attuale non è corretta."),
        ({"new": "short"}, "troppo corta"),
        ({"new": PW}, "deve essere diversa da quella attuale"),
        ({"confirm": "other-pw-123"}, "Le due password non coincidono."),
        ({"old": "", "new": "", "confirm": ""}, "Questo campo è obbligatorio."),
    ],
)
def test_change_password_rejections(client, user, kwargs, message):
    client.force_login(user)
    r = change(client, **kwargs)
    assert r.status_code == 200
    assert message in r.content.decode()
    user.refresh_from_db()
    assert user.check_password(PW)


def test_change_password_success(client, user):
    client.force_login(user)
    r = change(client)
    assert r.status_code == 302
    page = client.get("/password/")
    assert "Password modificata." in page.content.decode()
    user.refresh_from_db()
    assert user.check_password("brand-new-pw")
    client.post("/logout/")
    old = client.post("/login/", {"username": user.email, "password": PW})
    assert old.status_code == 200
    new = client.post("/login/", {"username": user.email, "password": "brand-new-pw"})
    assert new.status_code == 302


def test_change_password_invalidates_other_sessions(client, user):
    other = Client()
    other.force_login(user)
    assert other.get("/password/").status_code == 200
    client.force_login(user)
    change(client)
    assert client.get("/password/").status_code == 200
    r = other.get("/password/")
    assert r.status_code == 302
    assert r.url.startswith("/login/")


def run(*args, **kwargs):
    out = StringIO()
    call_command("create_user", *args, stdout=out, **kwargs)
    return out.getvalue()


def test_command_creates_user(db):
    out = run("New@Example.com", role="ADMIN", password="longenough")
    u = User.objects.get(email="new@example.com")
    assert u.role == "ADMIN"
    assert u.check_password("longenough")
    assert "Created" in out


def test_command_prompts_for_password(db, monkeypatch):
    monkeypatch.setattr("getpass.getpass", lambda prompt: "prompted-pw")
    run("p@example.com", role="SEGRETERIA")
    assert User.objects.get(email="p@example.com").check_password("prompted-pw")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"role": "BOSS", "password": "longenough"},
        {"role": "ADMIN", "password": "short"},
    ],
)
def test_command_rejects_invalid_input(db, kwargs):
    with pytest.raises(CommandError):
        run("bad@example.com", **kwargs)
    assert not User.objects.exists()


def test_command_rejects_invalid_email(db):
    with pytest.raises(CommandError):
        run("not-an-email", role="ADMIN", password="longenough")
    assert not User.objects.exists()


def test_command_existing_user_is_reset(user):
    out = run("MARIO@example.com", role="ADMIN", password="another-pw-1")
    assert User.objects.count() == 1
    user.refresh_from_db()
    assert user.role == "ADMIN"
    assert user.check_password("another-pw-1")
    assert "already existed" in out
