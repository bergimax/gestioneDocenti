import datetime
import logging
import re

import pytest
from django.db import connection
from django.test import Client

from accounts.models import User
from availability import links
from availability.models import AvailabilityLink, LinkLookupFailure
from school.models import School, Teacher

PW = "correct-horse"
D = datetime.date
W1 = D(2026, 9, 7)
T0 = datetime.datetime(2026, 10, 1, 12, 0, tzinfo=datetime.UTC)


def gen_url(week, tid):
    return f"/disponibilita/{week:%Y-%m-%d}/docente/{tid}/link/"


@pytest.fixture
def school(db):
    return School.objects.create(
        name="S",
        formative_year="2026-2027",
        year_start=D(2026, 9, 1),
        year_end=D(2027, 6, 30),
    )


@pytest.fixture
def teacher(school):
    return Teacher.objects.create(
        school=school, name="ROSSI", type="CONTRATTO", first_time=False
    )


@pytest.fixture
def clock(monkeypatch):
    state = {"now": T0}
    monkeypatch.setattr(links, "now", lambda: state["now"])
    return state


def login(role):
    u = User.objects.create_user(f"{role.lower()}@example.com", PW, role)
    c = Client()
    c.force_login(u)
    return c


@pytest.fixture
def admin(db):
    return login("ADMIN")


def make(teacher, week=W1):
    return links.create_link(teacher, week, None)


def token_from(response):
    return re.search(r"/l/([A-Za-z0-9_-]{43})/", response.content.decode()).group(1)


# generation and the shown-once URL


def test_generate_shows_url_once_with_expiry_and_no_store(admin, teacher, clock):
    r = admin.post(gen_url(W1, teacher.pk))
    assert r.status_code == 200
    assert "no-store" in r["Cache-Control"]
    body = r.content.decode()
    token = token_from(r)
    assert f"http://testserver/l/{token}/" in body
    assert "Copia" in body
    assert "Scade il 04/10/2026 alle 14:00" in body  # 72h after 12:00 UTC, Rome


def test_link_never_shown_again(admin, teacher, clock):
    token = token_from(admin.post(gen_url(W1, teacher.pk)))
    for path in (
        gen_url(W1, teacher.pk),
        "/disponibilita/link/",
        f"/disponibilita/?settimana={W1:%Y-%m-%d}",
    ):
        r = admin.get(path)
        assert token not in r.content.decode()
    listing = admin.get("/disponibilita/link/")
    assert "attivo" in listing.content.decode()
    assert "Revoca" in listing.content.decode()


def test_action_present_on_screen(admin, teacher):
    r = admin.get(f"/disponibilita/?settimana={W1:%Y-%m-%d}")
    assert gen_url(W1, teacher.pk) in r.content.decode()
    assert "Genera link" in r.content.decode()


def test_only_hash_stored(teacher):
    link, token = make(teacher)
    assert len(link.token_hash) == 64
    assert link.token_hash == links.hash_token(token)
    with connection.cursor() as cur:
        cur.execute("select * from availability_availabilitylink")
        row = [str(v) for v in cur.fetchone()]
    assert token not in " ".join(row)
    assert token[:6] not in " ".join(row)


def test_tokens_differ_and_hash_unique(teacher):
    a, ta = make(teacher, W1)
    b, tb = make(teacher, D(2026, 9, 14))
    assert ta != tb and a.token_hash != b.token_hash
    assert AvailabilityLink._meta.get_field("token_hash").unique


def test_second_link_revokes_first(teacher):
    a, ta = make(teacher)
    b, tb = make(teacher)
    assert links.lookup_link(ta) is None
    assert links.lookup_link(tb).pk == b.pk
    a.refresh_from_db()
    assert a.revoked_at is not None


# lookup


@pytest.mark.parametrize(
    "bad", ["", "abc", "x" * 43 + "y", "!" * 43, "a b" * 15, "é" * 43, None]
)
def test_malformed_tokens_invalid(teacher, bad):
    make(teacher)
    assert links.lookup_link(bad) is None


def test_unknown_token_invalid(teacher):
    assert links.lookup_link("A" * 43) is None


def test_expiry_boundary(teacher, clock):
    link, token = make(teacher)
    clock["now"] = link.expires_at - datetime.timedelta(seconds=1)
    assert links.lookup_link(token) is not None
    clock["now"] = link.expires_at
    assert links.lookup_link(token) is None
    clock["now"] = link.expires_at + datetime.timedelta(seconds=1)
    assert links.lookup_link(token) is None


def test_ttl_from_settings(teacher, clock, settings):
    settings.AVAILABILITY_LINK_TTL_HOURS = 1
    link, _ = make(teacher)
    assert link.expires_at - T0 == datetime.timedelta(hours=1)


def test_used_at_set_on_first_open(client, teacher, clock):
    link, token = make(teacher)
    assert client.get(f"/l/{token}/").status_code == 200
    link.refresh_from_db()
    first = link.used_at
    assert first == T0
    clock["now"] = T0 + datetime.timedelta(hours=1)
    assert client.get(f"/l/{token}/").status_code == 200
    link.refresh_from_db()
    assert link.used_at == first


@pytest.mark.parametrize("value", ["0", "-1", "abc"])
def test_bad_ttl_fails_startup(value):
    from tests.test_settings import _check

    result = _check({"AVAILABILITY_LINK_TTL_HOURS": value})
    assert result.returncode != 0
    assert "AVAILABILITY_LINK_TTL_HOURS" in result.stderr


# revocation


def test_revoke_stops_link_and_is_idempotent(admin, client, teacher):
    link, token = make(teacher)
    r = admin.post(f"/disponibilita/link/{link.pk}/revoca/")
    assert r.status_code == 302
    assert client.get(f"/l/{token}/").status_code == 404
    link.refresh_from_db()
    first = link.revoked_at
    r = admin.post(f"/disponibilita/link/{link.pk}/revoca/", follow=True)
    assert r.status_code == 200
    link.refresh_from_db()
    assert link.revoked_at == first
    assert admin.post("/disponibilita/link/99999/revoca/").status_code == 302


def test_revoke_expired_harmless(admin, teacher, clock):
    link, _ = make(teacher)
    clock["now"] = link.expires_at + datetime.timedelta(hours=1)
    r = admin.post(f"/disponibilita/link/{link.pk}/revoca/", follow=True)
    assert r.status_code == 200
    link.refresh_from_db()
    assert link.revoked_at is None
    assert "scaduto" in admin.get("/disponibilita/link/").content.decode()


# rate limit


def test_rate_limit(client, teacher, clock):
    link, good = make(teacher)
    for _ in range(10):
        assert client.get("/l/" + "A" * 43 + "/").status_code == 404
    r = client.get("/l/" + "A" * 43 + "/")
    assert r.status_code == 429
    assert client.get(f"/l/{good}/").status_code == 429
    other = client.get("/l/" + "A" * 43 + "/", REMOTE_ADDR="10.0.0.9")
    assert other.status_code == 404
    clock["now"] = T0 + datetime.timedelta(seconds=301)
    assert client.get(f"/l/{good}/").status_code == 200


def test_valid_lookups_not_counted(client, teacher):
    _, good = make(teacher)
    for _ in range(15):
        assert client.get(f"/l/{good}/").status_code == 200
    assert LinkLookupFailure.objects.count() == 0


def test_rate_limit_values_from_settings(client, teacher, settings):
    settings.LINK_LOOKUP_MAX_FAILURES = 2
    for _ in range(2):
        assert client.get("/l/" + "A" * 43 + "/").status_code == 404
    assert client.get("/l/" + "A" * 43 + "/").status_code == 429


# permissions


@pytest.mark.parametrize("role", ["ADMIN", "SEGRETERIA"])
def test_both_roles_manage(role, teacher):
    c = login(role)
    r = c.post(gen_url(W1, teacher.pk))
    assert r.status_code == 200
    link = AvailabilityLink.objects.get()
    assert c.get("/disponibilita/link/").status_code == 200
    c.post(f"/disponibilita/link/{link.pk}/revoca/")
    link.refresh_from_db()
    assert link.revoked_at is not None
    assert link.created_by.role == role


def test_anonymous_redirected_nothing_created(client, teacher):
    link, _ = make(teacher)
    r = client.post(gen_url(D(2026, 9, 14), teacher.pk))
    assert r.status_code == 302 and r.url.startswith("/login/")
    assert client.get("/disponibilita/link/").status_code == 302
    assert client.post(f"/disponibilita/link/{link.pk}/revoca/").status_code == 302
    link.refresh_from_db()
    assert link.revoked_at is None
    assert AvailabilityLink.objects.count() == 1


# invalid requests


def test_invalid_requests_refused(admin, school, teacher):
    inactive = Teacher.objects.create(
        school=school, name="BIANCHI", type="CONTRATTO", active=False
    )
    cases = [
        (W1, inactive.pk),
        (D(2026, 9, 8), teacher.pk),  # Tuesday
        (D(2025, 9, 1), teacher.pk),  # outside year
        (D(2027, 9, 6), teacher.pk),
        (W1, 99999),
    ]
    for week, tid in cases:
        r = admin.post(gen_url(week, tid))
        assert r.status_code == 400
        assert 'role="alert"' in r.content.decode()
    r = admin.post("/disponibilita/notadate/docente/1/link/")
    assert r.status_code == 400
    assert AvailabilityLink.objects.count() == 0


def test_other_school_teacher_refused(admin, teacher):
    other = School.objects.create(
        name="X",
        formative_year="2026-2027",
        year_start=D(2026, 9, 1),
        year_end=D(2027, 6, 30),
    )
    t = Teacher.objects.create(school=other, name="ALTRO", type="CONTRATTO")
    assert admin.post(gen_url(W1, t.pk)).status_code == 400


# logging


def test_token_never_logged(admin, client, teacher, clock, caplog):
    caplog.set_level(logging.DEBUG)
    logging.getLogger("django.request").setLevel(logging.DEBUG)
    token = token_from(admin.post(gen_url(W1, teacher.pk)))
    link = AvailabilityLink.objects.get()
    assert client.get(f"/l/{token}/").status_code == 200
    unknown = "B" * 43
    assert client.get(f"/l/{unknown}/").status_code == 404
    clock["now"] = link.expires_at + datetime.timedelta(seconds=1)
    assert client.get(f"/l/{token}/").status_code == 404
    clock["now"] = T0
    for _ in range(11):
        client.get(f"/l/{unknown}/")
    assert client.get(f"/l/{unknown}/").status_code == 429
    assert any(r.name == "django.request" for r in caplog.records)
    for secret in (token, unknown):
        for record in caplog.records:
            assert secret not in record.getMessage()
            assert secret not in str(record.__dict__.get("args"))
            assert secret not in str(record.__dict__.get("msg"))
