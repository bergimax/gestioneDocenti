import datetime
import logging

import pytest
from django.test import Client

from accounts.models import User
from availability.logging import redact
from feeds.ics import fold
from feeds.models import FeedToken
from feeds.services import ensure_tokens
from projects import permissions as p
from scheduling import audit
from scheduling.models import AuditLog, Slot, Timetable, TimetableState
from scheduling.services import approve_timetable, undo_approval
from school.models import CalendarEntry, Class, School, Subject, Teacher

D = datetime.date
MON = D(2026, 10, 19)


@pytest.fixture
def w(db):
    class W:
        pass

    w = W()
    w.school = School.objects.create(
        name="Scuola",
        formative_year="2026-2027",
        year_start=D(2026, 9, 1),
        year_end=D(2027, 6, 30),
    )
    w.c1a = Class.objects.create(school=w.school, name="1A")
    w.c1b = Class.objects.create(school=w.school, name="1B")
    w.rossi = Teacher.objects.create(
        school=w.school, name="ROSSI MARIO", type="ASSUNTO", email="x@example.org"
    )
    w.verdi = Teacher.objects.create(school=w.school, name="VERDI", type="ASSUNTO")
    w.mat = Subject.objects.create(
        school=w.school, name="Matematica", kind="TEORIA", cognitive_weight="MEDIO"
    )
    w.tt = Timetable.objects.create(
        school=w.school, week_start=MON, state=TimetableState.APPROVATO
    )
    w.admin = User.objects.create_user("admin@example.com", "pw-long-enough", "ADMIN")
    return w


def slot(
    w, klass=None, day="MARTEDI", hour=8, tt=None, teacher=None, subject=None, **kw
):
    return Slot.objects.create(
        timetable=tt or w.tt,
        school_class=klass or w.c1a,
        teacher=teacher or w.rossi,
        subject=subject or w.mat,
        day=day,
        start_hour=hour,
        end_hour=hour + 1,
        **kw,
    )


def login(user):
    c = Client()
    c.force_login(user)
    return c


def token_of(obj):
    ensure_tokens(obj.school)
    obj.refresh_from_db()
    return obj.feed_token.token


def get_feed(obj):
    return Client().get(f"/feeds/{token_of(obj)}.ics")


def unfold(body):
    assert body.endswith("\r\n")
    assert "\n" not in body.replace("\r\n", "")
    lines = []
    for raw in body[:-2].split("\r\n"):
        assert len(raw.encode()) <= 75
        if raw.startswith(" "):
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def events(response):
    lines = unfold(response.content.decode())
    assert lines[:2] == ["BEGIN:VCALENDAR", "VERSION:2.0"]
    assert lines[-1] == "END:VCALENDAR"
    out, cur = [], None
    for line in lines:
        if line == "BEGIN:VEVENT":
            cur = {}
        elif line == "END:VEVENT":
            out.append(cur)
            cur = None
        elif cur is not None:
            key, _, value = line.partition(":")
            cur[key] = value
    assert cur is None
    return out


# ---- public feed ------------------------------------------------------------


def test_feed_is_a_valid_calendar_without_login(w):
    slot(w)
    r = get_feed(w.rossi)
    assert r.status_code == 200
    assert r["Content-Type"] == "text/calendar; charset=utf-8"
    lines = unfold(r.content.decode())
    assert "PRODID:-//gestioneDocenti//Orario//IT" in lines
    assert "CALSCALE:GREGORIAN" in lines
    assert "X-WR-CALNAME:ROSSI MARIO" in lines
    assert len(events(r)) == 1
    assert r["Cache-Control"] == "max-age=300"
    assert r["X-Robots-Tag"] == "noindex"


def test_tokens_are_long_random_and_unique(w):
    ensure_tokens(w.school)
    tokens = list(FeedToken.objects.values_list("token", flat=True))
    assert len(tokens) == len(set(tokens)) == 4
    assert all(len(t) == 43 for t in tokens)


def test_bad_tokens_get_the_same_404_and_post_is_405(w):
    good = token_of(w.rossi)
    c = Client()
    bodies = {
        c.get(f"/feeds/{t}.ics").content
        for t in ("B" * 43, "short", "B" * 44, "!" * 43, good[:-1] + "!")
    }
    assert len(bodies) == 1
    for t in ("B" * 43, "short"):
        assert c.get(f"/feeds/{t}.ics").status_code == 404
    assert c.post(f"/feeds/{good}.ics").status_code == 405


def test_inactive_teacher_is_404_and_not_listed(w):
    url = f"/feeds/{token_of(w.verdi)}.ics"
    assert Client().get(url).status_code == 200
    w.verdi.active = False
    w.verdi.save()
    assert Client().get(url).status_code == 404
    page = login(w.admin).get("/feeds/").content.decode()
    assert "VERDI" not in page and "ROSSI MARIO" in page


def test_empty_feed_has_no_event(w):
    r = get_feed(w.verdi)
    assert r.status_code == 200 and events(r) == []
    Timetable.objects.all().delete()
    assert events(get_feed(w.verdi)) == []


def test_only_approved_weeks_and_undo_cycle_keeps_uids(w):
    draft = Timetable.objects.create(
        school=w.school, week_start=MON + datetime.timedelta(days=7)
    )
    slot(w, tt=draft)
    slot(w)
    first = [e["UID"] for e in events(get_feed(w.rossi))]
    assert len(first) == 1
    undo_approval(w.tt.pk, w.admin, "errore")
    assert events(get_feed(w.rossi)) == []
    approve_timetable(w.tt.pk, w.admin)
    assert [e["UID"] for e in events(get_feed(w.rossi))] == first
    assert first == [f"2026-10-19-{w.c1a.pk}-MARTEDI-8-{w.rossi.pk}@gestionedocenti"]


def test_teacher_and_class_feeds_with_paired_lessons(w):
    slot(w, hour=9)
    slot(w, hour=10, paired=True, paired_class=w.c1b)
    slot(w, klass=w.verdi and w.c1b, hour=11, teacher=w.verdi)
    teacher = events(get_feed(w.rossi))
    assert len(teacher) == 2
    assert {e["SUMMARY"] for e in teacher} == {
        "Matematica - 1A",
        "Matematica - 1A + 1B",
    }
    assert len(events(get_feed(w.c1a))) == 2
    b = events(get_feed(w.c1b))
    assert {e["SUMMARY"] for e in b} == {
        "Matematica - ROSSI MARIO",
        "Matematica - VERDI",
    }
    uids = [e["UID"] for e in teacher]
    assert len(uids) == len(set(uids))


def test_no_teacher_email_in_any_feed(w):
    slot(w)
    for obj in (w.rossi, w.c1a):
        assert "example.org" not in get_feed(obj).content.decode()


def test_times_in_utc_around_dst_changes(w):
    slot(w, day="MARTEDI", hour=8)  # 2026-10-20, CEST
    after = Timetable.objects.create(
        school=w.school,
        week_start=D(2026, 10, 26),
        state=TimetableState.APPROVATO,
    )
    slot(w, day="LUNEDI", hour=8, tt=after)  # CET
    march = Timetable.objects.create(
        school=w.school, week_start=D(2027, 3, 22), state=TimetableState.APPROVATO
    )
    slot(w, day="VENERDI", hour=8, tt=march)  # 2027-03-26 CET
    next_week = Timetable.objects.create(
        school=w.school, week_start=D(2027, 3, 29), state=TimetableState.APPROVATO
    )
    slot(w, day="LUNEDI", hour=8, tt=next_week)  # 2027-03-29 CEST
    ev = events(get_feed(w.rossi))
    assert [(e["DTSTART"], e["DTEND"]) for e in ev] == [
        ("20261020T060000Z", "20261020T070000Z"),
        ("20261026T070000Z", "20261026T080000Z"),
        ("20270326T070000Z", "20270326T080000Z"),
        ("20270329T060000Z", "20270329T070000Z"),
    ]


def test_stage_and_closure_days_have_no_event(w):
    CalendarEntry.objects.create(school=w.school, date=D(2026, 10, 21), is_closure=True)
    CalendarEntry.objects.create(
        school=w.school, date=D(2026, 10, 22), stage_class=w.c1a
    )
    slot(w, day="LUNEDI")
    slot(w, day="VENERDI")
    ev = events(get_feed(w.rossi))
    assert [e["DTSTART"][:8] for e in ev] == ["20261019", "20261023"]
    assert "STAGE" not in get_feed(w.rossi).content.decode()


def test_escaping_folding_and_dtstamp(w):
    subject = Subject.objects.create(
        school=w.school,
        name="Storia, geografia; cittadinanza",
        kind="TEORIA",
        cognitive_weight="BASSO",
    )
    slot(w, subject=subject)
    long_name = Subject.objects.create(
        school=w.school,
        name="Lungo " * 30 + "è",
        kind="TEORIA",
        cognitive_weight="BASSO",
    )
    slot(w, subject=long_name, hour=9)
    r = get_feed(w.rossi)
    assert b"SUMMARY:Storia\\, geografia\\; cittadinanza - 1A\r\n" in r.content
    ev = events(r)
    assert all(e["DTSTAMP"].endswith("Z") for e in ev)
    assert ev[1]["SUMMARY"].startswith("Lungo ")
    assert fold("A" * 200).count("\r\n") == 2


def test_query_count_is_constant(w, django_assert_max_num_queries):
    token = token_of(w.rossi)
    for i in range(40):
        tt = Timetable.objects.create(
            school=w.school,
            week_start=D(2026, 11, 2) + datetime.timedelta(days=7 * i),
            state=TimetableState.APPROVATO,
        )
        for hour in (8, 9, 10, 11, 12, 13):
            slot(w, tt=tt, hour=hour, paired=True, paired_class=w.c1b)
    with django_assert_max_num_queries(3):
        r = Client().get(f"/feeds/{token}.ics")
    assert len(events(r)) == 240


# ---- admin screen -----------------------------------------------------------


def test_screen_lists_urls_and_creates_tokens_lazily(w):
    assert FeedToken.objects.count() == 0
    r = login(w.admin).get("/feeds/")
    assert r.status_code == 200
    page = r.content.decode()
    for obj in (w.rossi, w.verdi, w.c1a, w.c1b):
        obj.refresh_from_db()
        assert f"http://testserver/feeds/{obj.feed_token.token}.ics" in page
    assert "Rigenera" in page and "Copia" in page
    assert "smettera di funzionare" in page
    # a later class gets its token on the next visit, existing ones unchanged
    before = {f.pk: f.token for f in FeedToken.objects.all()}
    Class.objects.create(school=w.school, name="2A")
    login(w.admin).get("/feeds/")
    assert FeedToken.objects.count() == 5
    assert all(FeedToken.objects.get(pk=k).token == v for k, v in before.items())


def test_regenerate_replaces_only_that_token_and_audits(w):
    old = token_of(w.rossi)
    other = token_of(w.c1a)
    c = login(w.admin)
    assert c.get(f"/feeds/rigenera/teacher/{w.rossi.pk}/").status_code == 405
    r = c.post(f"/feeds/rigenera/teacher/{w.rossi.pk}/")
    assert r.status_code == 302 and r.url == "/feeds/"
    assert Client().get(f"/feeds/{old}.ics").status_code == 404
    w.rossi.feed_token.refresh_from_db()
    new = w.rossi.feed_token.token
    assert new != old and Client().get(f"/feeds/{new}.ics").status_code == 200
    assert Client().get(f"/feeds/{other}.ics").status_code == 200
    entry = AuditLog.objects.get(action=audit.FEED_REGENERATED)
    assert entry.details == {"tipo": "teacher", "id": w.rossi.pk}
    assert old not in str(entry.details) and new not in str(entry.details)
    assert "ROSSI" not in str(entry.details)
    assert c.post(f"/feeds/rigenera/class/{w.c1a.pk}/").status_code == 302
    assert AuditLog.objects.filter(action=audit.FEED_REGENERATED).count() == 2
    class_entry = AuditLog.objects.filter(action=audit.FEED_REGENERATED).first()
    assert class_entry.details == {"tipo": "class", "id": w.c1a.pk}
    assert w.c1a.name not in str(class_entry.details)
    assert c.post("/feeds/rigenera/class/99999/").status_code == 404
    assert c.post("/feeds/rigenera/boh/1/").status_code == 404
    page = c.get("/audit/").content.decode()
    assert "Rigenerazione feed" in page and new not in page


def test_regeneration_audit_rolls_back_with_the_token(w, monkeypatch):
    old = token_of(w.rossi)

    def boom(*a, **k):
        raise RuntimeError

    monkeypatch.setattr("feeds.services.audit.record_audit", boom)
    with pytest.raises(RuntimeError):
        login(w.admin).post(f"/feeds/rigenera/teacher/{w.rossi.pk}/")
    w.rossi.feed_token.refresh_from_db()
    assert w.rossi.feed_token.token == old


# ---- permissions ------------------------------------------------------------


def test_can_manage_feeds_per_role(w):
    from django.contrib.auth.models import AnonymousUser

    seg = User.objects.create_user("seg@example.com", "pw-long-enough", "SEGRETERIA")
    assert p.can_manage_feeds(w.admin) is True
    assert p.can_manage_feeds(seg) is False
    assert p.can_manage_feeds(AnonymousUser()) is False


def test_segreteria_and_anonymous_are_kept_out(w):
    seg = User.objects.create_user("seg@example.com", "pw-long-enough", "SEGRETERIA")
    token = token_of(w.rossi)
    c = login(seg)
    assert c.get("/feeds/").status_code == 403
    r = c.post(f"/feeds/rigenera/teacher/{w.rossi.pk}/")
    assert r.status_code == 403 and token not in r.content.decode()
    assert "/feeds/" not in c.get("/").content.decode()
    anon = Client()
    for r in (anon.get("/feeds/"), anon.post(f"/feeds/rigenera/teacher/{w.rossi.pk}/")):
        assert r.status_code == 302 and r.url.startswith("/login/")
    assert 'href="/feeds/"' in login(w.admin).get("/").content.decode()
    w.rossi.feed_token.refresh_from_db()
    assert w.rossi.feed_token.token == token


# ---- logging ----------------------------------------------------------------


def test_redaction_of_feed_paths():
    token = "abcdefGHIJ" + "k" * 33
    assert redact(f"Not Found: /feeds/{token}.ics") == "Not Found: /feeds/abcdef....ics"
    assert redact(f'"GET /feeds/{token}.ics HTTP/1.1" 200').count(token) == 0
    assert redact("/feeds/") == "/feeds/"
    assert redact("/feeds/rigenera/teacher/3/") == "/feeds/rigenera/teacher/3/"


def test_token_never_logged(w, caplog):
    caplog.set_level(logging.DEBUG)
    for name in ("django.request", "django.server"):
        logging.getLogger(name).setLevel(logging.DEBUG)
    old = token_of(w.rossi)
    unknown = "B" * 43
    anon = Client()
    assert anon.get(f"/feeds/{old}.ics").status_code == 200
    assert anon.get(f"/feeds/{unknown}.ics").status_code == 404
    assert (
        login(w.admin).post(f"/feeds/rigenera/teacher/{w.rossi.pk}/").status_code == 302
    )
    assert anon.get(f"/feeds/{old}.ics").status_code == 404
    w.rossi.feed_token.refresh_from_db()
    new = w.rossi.feed_token.token
    assert caplog.records
    for secret in (old, unknown, new):
        for record in caplog.records:
            assert secret not in record.getMessage()
            assert secret not in str(record.__dict__.get("args"))
            assert secret not in str(record.__dict__.get("msg"))
