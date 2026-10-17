import datetime
import logging
import re
import subprocess
import sys

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import Client
from django.utils import timezone

from accounts.models import User
from config.env import get_bool
from feeds.models import FeedLookupFailure
from feeds.services import ensure_tokens, pick_week
from scheduling.models import Slot, Timetable, TimetableState
from scheduling.services import undo_approval
from school.models import CalendarEntry, Class, School, Subject, Teacher

D = datetime.date
PAGE_URL = re.compile(r"/orario/[A-Za-z0-9_-]{43}/")
W1, W2, W3 = D(2026, 10, 12), D(2026, 10, 19), D(2026, 10, 26)


@pytest.fixture(autouse=True)
def on(settings, monkeypatch):
    settings.PUBLIC_WEEK_PAGE_ENABLED = True
    monkeypatch.setattr(timezone, "localdate", lambda *a, **k: D(2026, 10, 21))


@pytest.fixture
def w(db):
    class W:
        pass

    w = W()
    w.school = School.objects.create(
        name="Scuola Alfa",
        formative_year="2026-2027",
        year_start=D(2026, 9, 1),
        year_end=D(2027, 6, 30),
    )
    w.c1a = Class.objects.create(school=w.school, name="1A")
    w.c1b = Class.objects.create(school=w.school, name="1B")
    w.rossi = Teacher.objects.create(
        school=w.school, name="ROSSI MARIO", type="ASSUNTO", email="rossi@example.org"
    )
    w.mat = Subject.objects.create(
        school=w.school, name="Matematica", kind="TEORIA", cognitive_weight="MEDIO"
    )
    w.art = Subject.objects.create(
        school=w.school, name="Arte", kind="TEORIA", cognitive_weight="MEDIO"
    )
    w.draft = Subject.objects.create(
        school=w.school, name="Bozzaparola", kind="TEORIA", cognitive_weight="MEDIO"
    )
    w.tt = {}
    for start in (W1, W2, W3):
        w.tt[start] = Timetable.objects.create(
            school=w.school,
            week_start=start,
            state=TimetableState.APPROVATO,
            quality_score=87.5,
            conflicts=[{"description": "CONFLITTO SEGRETO"}],
        )
    w.bozza = Timetable.objects.create(school=w.school, week_start=D(2026, 11, 2))
    ensure_tokens(w.school)
    w.c1a.refresh_from_db()
    w.token = w.c1a.feed_token.token
    w.admin = User.objects.create_user("admin@example.com", "pw-long-enough", "ADMIN")
    return w


def lesson(w, tt, klass, subject, day="MARTEDI", hour=9, **kw):
    return Slot.objects.create(
        timetable=tt,
        school_class=klass,
        teacher=w.rossi,
        subject=subject,
        day=day,
        start_hour=hour,
        end_hour=hour + 1,
        **kw,
    )


def get(w, token=None, query="", client=None):
    return (client or Client()).get(f"/orario/{token or w.token}/{query}")


# ---- settings ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, False),
        ("", False),
        ("  ", False),
        ("true", True),
        ("1", True),
        ("false", False),
        ("0", False),
    ],
)
def test_switch_parsing(raw, expected):
    env = {} if raw is None else {"PUBLIC_WEEK_PAGE_ENABLED": raw}
    assert get_bool(env, "PUBLIC_WEEK_PAGE_ENABLED") is expected


def test_switch_yes_fails():
    with pytest.raises(ImproperlyConfigured):
        get_bool({"PUBLIC_WEEK_PAGE_ENABLED": "yes"}, "PUBLIC_WEEK_PAGE_ENABLED")


def test_switch_wired_and_documented():
    import os
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    for rel in (".env.example", "deploy/env.production.example"):
        text = (root / rel).read_text()
        assert "PUBLIC_WEEK_PAGE_ENABLED=false" in text
        assert "default false" in text
    bad = {**os.environ, "PUBLIC_WEEK_PAGE_ENABLED": "yes"}
    result = subprocess.run(
        [sys.executable, "-c", "import config.settings"],
        cwd=root,
        env=bad,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "PUBLIC_WEEK_PAGE_ENABLED" in result.stderr
    env = {k: v for k, v in os.environ.items() if k != "PUBLIC_WEEK_PAGE_ENABLED"}
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from config import settings; print(settings.PUBLIC_WEEK_PAGE_ENABLED)",
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "False", result.stderr


# ---- off by default ----------------------------------------------------------


def test_off_is_identical_to_unknown(w, settings):
    lesson(w, w.tt[W2], w.c1a, w.mat)
    settings.PUBLIC_WEEK_PAGE_ENABLED = False
    off = get(w)
    unknown = get(w, "B" * 43)
    assert off.status_code == unknown.status_code == 404
    assert off.content == unknown.content
    assert off["Content-Type"] == unknown["Content-Type"]
    page = Client()
    page.force_login(w.admin)
    assert not PAGE_URL.search(page.get("/feeds/").content.decode())


# ---- content -----------------------------------------------------------------


def test_page_shows_lessons(w):
    lesson(w, w.tt[W2], w.c1a, w.mat, "MARTEDI", 9)
    lesson(w, w.tt[W2], w.c1a, w.art, "VENERDI", 12)
    r = get(w, query="?week=2026-10-19")
    assert r.status_code == 200
    assert not r.cookies
    html = r.content.decode()
    assert "Scuola Alfa" in html and "1A" in html
    assert "Orario scolastico dal 19 al 23 Ottobre" in html
    rows = {row["label"]: row["cells"] for row in r.context["rows"]}
    assert rows["9-10"][1]["text"] == "Matematica"
    assert rows["12-13"][4]["text"] == "Arte"
    assert rows["8-9"][0]["kind"] == "empty"
    for header in ("Lunedì", "Venerdì"):
        assert header in html


def test_no_leak(w):
    t = w.tt[W2]
    lesson(w, t, w.c1a, w.mat, "MARTEDI", 9)
    lesson(w, t, w.c1b, w.art, "MARTEDI", 10)  # other class
    lesson(w, w.bozza, w.c1a, w.draft, "LUNEDI", 8)
    lesson(w, t, w.c1b, w.art, "GIOVEDI", 8, paired=True, paired_class=w.c1a)
    r = get(w, query="?week=2026-10-19")
    html = r.content.decode()
    for secret in (
        "ROSSI",
        "rossi@example.org",
        "1B",
        "87.5",
        "CONFLITTO",
        "Bozzaparola",
    ):
        assert secret not in html
        assert secret not in repr(r.context.flatten())
    rows = {row["label"]: row["cells"] for row in r.context["rows"]}
    assert rows["8-9"][3]["text"] == "Arte"  # paired lesson, subject only
    assert "Arte" in html  # shown for 1A through the pair
    other = get(w, w.c1b.feed_token.token, "?week=2026-10-19").content.decode()
    assert "Matematica" not in other and "Arte" in other


def test_stage_cells(w):
    CalendarEntry.objects.create(
        school=w.school, date=D(2026, 10, 20), stage_class=w.c1a
    )
    cells = get(w, query="?week=2026-10-19").context["rows"][0]["cells"]
    assert cells[1] == {"kind": "stage", "text": "STAGE"}
    other = get(w, w.c1b.feed_token.token, "?week=2026-10-19")
    assert other.context["rows"][0]["cells"][1]["kind"] == "empty"


# ---- approved only -----------------------------------------------------------


def test_bozza_unreachable_and_undo(w):
    assert get(w, query="?week=2026-11-02").status_code == 404
    lesson(w, w.tt[W2], w.c1a, w.mat)
    assert "Matematica" in get(w, query="?week=2026-10-19").content.decode()
    for start in (W1, W3):
        w.tt[start].delete()
    undo_approval(w.tt[W2].pk, w.admin, "errore")
    r = get(w, query="?week=2026-10-19")
    assert r.status_code == 404
    r = get(w)
    assert r.status_code == 200
    assert "Nessuna settimana approvata." in r.content.decode()
    assert "Matematica" not in r.content.decode()


def test_week_selection(w):
    r = get(w)  # today = Wed 21 Oct 2026
    assert r.context["title"].endswith("19 al 23 Ottobre")
    assert r.context["previous_week"] == W1 and r.context["next_week"] == W3
    html = r.content.decode()
    assert "?week=2026-10-12" in html and "?week=2026-10-26" in html
    assert "2026-11-02" not in html
    assert get(w, query="?week=2026-10-12").context["previous_week"] is None


def test_pick_week_rules():
    weeks = [W1, W2, W3]
    assert pick_week(weeks, D(2026, 10, 21)) == W2
    assert pick_week(weeks, D(2026, 10, 25)) == W2  # Sunday
    assert pick_week(weeks, D(2026, 10, 1)) == W1  # nearest later
    assert pick_week([W1, W3], D(2026, 10, 22)) == W3
    assert pick_week(weeks, D(2027, 1, 1)) == W3  # latest earlier
    assert pick_week([], D(2026, 1, 1)) is None


@pytest.mark.parametrize(
    "query",
    [
        "?week=2026-10-20",
        "?week=boh",
        "?week=2030-01-07",
        "?week=",
        "?week=20261019",
        "?week=2026-10-19&week=x",
    ],
)
def test_bad_week_is_404(w, query):
    unknown = get(w, "B" * 43)
    r = get(w, query=query)
    if query == "?week=2026-10-19&week=x":
        assert r.status_code == 404  # last value wins
    assert r.status_code == 404 and r.content == unknown.content


# ---- tokens ------------------------------------------------------------------


def test_token_failures_identical(w):
    teacher = w.rossi.feed_token.token
    unknown = get(w, "B" * 43)
    for token in (teacher, "short", "x" * 44, "B" * 43, "A" * 43):
        r = get(w, token)
        assert (r.status_code, r.content, r["Content-Type"]) == (
            404,
            unknown.content,
            unknown["Content-Type"],
        )


def test_regenerate_revokes(w):
    admin = Client()
    admin.force_login(w.admin)
    old = w.token
    assert admin.post(f"/feeds/rigenera/class/{w.c1a.pk}/").status_code == 302
    w.c1a.feed_token.refresh_from_db()
    assert get(w, old).status_code == 404
    assert get(w, w.c1a.feed_token.token).status_code == 200


def test_failures_are_counted_and_limited(w, settings):
    settings.FEED_LOOKUP_MAX_FAILURES = 2
    for _ in range(2):
        assert get(w, "B" * 43).status_code == 404
    assert FeedLookupFailure.objects.count() == 2
    r = get(w)  # valid token, but the address is limited
    assert r.status_code == 429 and "Retry-After" in r
    assert r["Cache-Control"] == "no-store"


def test_success_is_not_counted(w):
    get(w)
    assert FeedLookupFailure.objects.count() == 0


# ---- headers -----------------------------------------------------------------


def test_headers_and_methods(w):
    for r in (get(w), get(w, "B" * 43)):
        assert r["Cache-Control"] == "no-store"
        assert r["X-Robots-Tag"] == "noindex"
        assert r["Referrer-Policy"] == "no-referrer"
    html = get(w).content.decode()
    assert '<meta name="robots" content="noindex">' in html
    assert '<meta name="referrer" content="no-referrer">' in html
    for verb in ("post", "put", "delete"):
        assert getattr(Client(), verb)(f"/orario/{w.token}/").status_code == 405


# ---- admin screen ------------------------------------------------------------


def test_admin_screen_shows_page_urls(w):
    admin = Client()
    admin.force_login(w.admin)
    html = admin.get("/feeds/").content.decode()
    assert len(PAGE_URL.findall(html)) == 2  # one input per class, none for teachers
    assert f"/orario/{w.token}/" in html
    assert f"/feeds/{w.token}.ics" in html
    assert f"/orario/{w.rossi.feed_token.token}/" not in html


# ---- queries, logging --------------------------------------------------------


def test_query_count_is_flat(w, django_assert_max_num_queries):
    lesson(w, w.tt[W2], w.c1a, w.mat, "LUNEDI", 8)
    lesson(w, w.bozza, w.c1a, w.draft, "LUNEDI", 9)
    with django_assert_max_num_queries(6):
        get(w, query="?week=2026-10-19")
    for day in ("MARTEDI", "MERCOLEDI", "GIOVEDI", "VENERDI"):
        for hour in range(8, 14):
            lesson(w, w.tt[W2], w.c1a, w.art, day, hour)
    with django_assert_max_num_queries(6):
        r = get(w, query="?week=2026-10-19")
    assert "Bozzaparola" not in r.content.decode()


def test_token_never_logged(w, caplog):
    caplog.set_level(logging.DEBUG)
    for name in ("django.request", "django.server"):
        logging.getLogger(name).setLevel(logging.DEBUG)
    unknown = "B" * 43
    assert get(w).status_code == 200
    assert get(w, unknown).status_code == 404
    assert get(w, query="?week=2026-10-20").status_code == 404
    assert caplog.records
    for secret in (w.token, unknown):
        for record in caplog.records:
            assert secret not in record.getMessage()
            assert secret not in str(record.__dict__.get("args"))
