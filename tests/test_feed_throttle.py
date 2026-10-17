import datetime
import logging

import pytest
from django.test import Client
from django.utils import timezone

from accounts import throttle as login_throttle
from accounts.models import LoginFailure
from availability import links
from availability.models import LinkLookupFailure
from feeds import throttle
from feeds.models import FeedLookupFailure
from feeds.services import ensure_tokens
from school.models import Class, School, Teacher

D = datetime.date
T0 = datetime.datetime(2026, 10, 1, 12, 0, tzinfo=datetime.UTC)
MAX = 10
WINDOW = 300
BAD = "A" * 43
LIMITED_BODY = "Troppi tentativi. Riprova piu tardi."


@pytest.fixture
def w(db):
    class W:
        pass

    w = W()
    w.school = School.objects.create(
        name="S",
        formative_year="2026-2027",
        year_start=D(2026, 9, 1),
        year_end=D(2027, 6, 30),
    )
    w.klass = Class.objects.create(school=w.school, name="1A")
    w.teacher = Teacher.objects.create(school=w.school, name="ROSSI", type="ASSUNTO")
    ensure_tokens(w.school)
    w.teacher.refresh_from_db()
    w.klass.refresh_from_db()
    w.t_tok = w.teacher.feed_token.token
    w.c_tok = w.klass.feed_token.token
    return w


@pytest.fixture
def clock(monkeypatch):
    state = {"now": T0}
    monkeypatch.setattr(throttle.timezone, "now", lambda: state["now"])
    return state


def get(token, **extra):
    return Client().get(f"/feeds/{token}.ics", **extra)


def fail_until_limited():
    c = Client()
    for i in range(MAX):
        tok = ("A" * 43, "short", "B" * 43, "x" * 60)[i % 4]
        assert c.get(f"/feeds/{tok}.ics").status_code == 404


def test_threshold(w, clock):
    first = get(BAD)
    assert first.status_code == 404
    first_body = first.content
    FeedLookupFailure.objects.all().delete()
    assert first_body == b"Feed non trovato."
    fail_until_limited()
    r = get(BAD)
    assert r.status_code == 429
    assert r["Retry-After"] == str(WINDOW)
    assert r.content.decode() == LIMITED_BODY
    assert r["Content-Type"] == "text/plain; charset=utf-8"
    assert FeedLookupFailure.objects.count() == MAX


def test_lockout_covers_valid_tokens_other_address_free(w, clock):
    fail_until_limited()
    assert get(w.t_tok).status_code == 429
    assert get(w.c_tok).status_code == 429
    assert get(w.t_tok, REMOTE_ADDR="10.0.0.9").status_code == 200
    assert get(BAD, REMOTE_ADDR="10.0.0.9").status_code == 404


def test_valid_lookups_not_counted(w):
    c = Client()
    for _ in range(3 * MAX):
        assert c.get(f"/feeds/{w.t_tok}.ics").status_code == 200
        assert c.get(f"/feeds/{w.c_tok}.ics").status_code == 200
    assert FeedLookupFailure.objects.count() == 0


def test_window_and_expiry(w, clock):
    fail_until_limited()
    assert get(w.t_tok).status_code == 429
    clock["now"] = T0 + datetime.timedelta(seconds=WINDOW + 1)
    assert get(w.t_tok).status_code == 200
    assert FeedLookupFailure.objects.count() == MAX
    assert get(BAD).status_code == 404
    assert FeedLookupFailure.objects.count() == 1


def test_inactive_teacher_old_token_and_post(w):
    Teacher.objects.filter(pk=w.teacher.pk).update(active=False)
    assert get(w.t_tok).status_code == 404
    assert FeedLookupFailure.objects.count() == 1
    Teacher.objects.filter(pk=w.teacher.pk).update(active=True)
    from accounts.models import User
    from feeds.services import regenerate

    admin = User.objects.create_user("a@example.com", "pw-long-enough", "ADMIN")
    regenerate(w.school, "class", w.klass.pk, admin)
    assert get(w.c_tok).status_code == 404
    assert FeedLookupFailure.objects.count() == 2
    assert Client().post(f"/feeds/{w.t_tok}.ics").status_code == 405
    assert Client().post(f"/feeds/{BAD}.ics").status_code == 405
    assert FeedLookupFailure.objects.count() == 2


def test_no_token_stored_or_logged(w, caplog):
    caplog.set_level(logging.DEBUG)
    for name in ("django.request", "django.server"):
        logging.getLogger(name).setLevel(logging.DEBUG)
    marker = "MARKERtokenMARKERtokenMARKERtokenMARKERtoke"
    assert len(marker) == 43
    for _ in range(MAX):
        assert get(marker).status_code == 404
    assert get(marker).status_code == 429
    names = {f.name for f in FeedLookupFailure._meta.get_fields()}
    assert names == {"id", "client_address", "attempted_at"}
    for row in FeedLookupFailure.objects.all():
        stored = f"{row.client_address} {row.attempted_at}"
        for i in range(0, len(marker) - 3):
            assert marker[i : i + 4] not in stored
    text = caplog.text
    assert marker not in text
    assert f"/feeds/{marker}.ics" not in text


def test_real_client_address(w, settings):
    settings.TRUSTED_PROXY_COUNT = 1
    get(BAD, HTTP_X_FORWARDED_FOR="9.9.9.9")
    get(BAD, HTTP_X_FORWARDED_FOR="1.1.1.1, 9.9.9.9")
    assert list(FeedLookupFailure.objects.values_list("client_address", flat=True)) == [
        "9.9.9.9",
        "9.9.9.9",
    ]
    settings.TRUSTED_PROXY_COUNT = 0
    FeedLookupFailure.objects.all().delete()
    get(BAD, HTTP_X_FORWARDED_FOR="9.9.9.9")
    assert FeedLookupFailure.objects.get().client_address == "127.0.0.1"


def test_helper_direct_and_truncation(db):
    assert not throttle.is_limited("5.5.5.5")
    for _ in range(MAX):
        throttle.record_failure("5.5.5.5")
    assert throttle.is_limited("5.5.5.5")
    assert not throttle.is_limited("5.5.5.6")
    long = "9" * 100
    throttle.record_failure(long)
    assert FeedLookupFailure.objects.filter(client_address=long[:64]).count() == 1


def test_independence(w):
    now = timezone.now()
    for _ in range(MAX):
        LinkLookupFailure.objects.create(client_address="127.0.0.1", attempted_at=now)
        LoginFailure.objects.create(
            email="x@example.com", client_address="127.0.0.1", attempted_at=now
        )
    assert not throttle.is_limited("127.0.0.1")
    assert get(BAD).status_code == 404
    assert get(w.t_tok).status_code == 200
    # and the other way round: feed failures do not lock links or login
    for _ in range(MAX):
        throttle.record_failure("10.1.1.1")
    assert throttle.is_limited("10.1.1.1")
    assert not links.is_rate_limited("10.1.1.1")
    assert not login_throttle.is_locked("y@example.com", "10.1.1.1")
