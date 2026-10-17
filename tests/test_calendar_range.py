import html as htmllib
import re
from unittest import mock

import pytest
from django.db import IntegrityError, connection
from django.test.utils import CaptureQueriesContext
from test_calendar_screens import (
    GROUPS,
    HX,
    MONTH,
    TODAY,
    d,
    entry,
    login,
    make_school,
    page,
)

from school.calendar import resolve_hours
from school.forms import CalendarEntryForm
from school.models import CalendarEntry, Class


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    monkeypatch.setattr("school.selectors.today", lambda: TODAY)


@pytest.fixture
def school(db):
    return make_school()


@pytest.fixture
def classes(school):
    return {
        g: Class.objects.create(school=school, name=f"{i + 1}A", group=g)
        for i, g in enumerate(GROUPS)
    }


@pytest.fixture
def admin(db):
    return login("ADMIN")


@pytest.fixture
def seg(db):
    return login("SEGRETERIA")


RANGE = "/dati-scuola/calendario/intervallo/"


def post(client, **kw):
    data = {
        "start": "2026-10-05",
        "end": "2026-10-23",
        "group": "QUARTE",
        "kind": "group_stage",
    }
    data.update(kw)
    return client.post(RANGE, data)


def count():
    return CalendarEntry.objects.count()


def body(response):
    return htmllib.unescape(response.content.decode())


# --- access --------------------------------------------------------------


def test_admin_sees_link_and_form(school, admin):
    assert "Applica a un intervallo" in page(admin)
    r = admin.get(RANGE)
    assert r.status_code == 200
    assert "<html" in body(r)
    r = admin.get(RANGE, **HX)
    assert r.status_code == 200
    assert "<html" not in body(r)
    assert r["HX-Retarget"] == "#calendar-form-area"


def test_segreteria_has_no_link_and_gets_403(school, seg):
    assert "Applica a un intervallo" not in page(seg)
    assert seg.get(RANGE).status_code == 403
    assert post(seg).status_code == 403
    assert count() == 0


def test_anonymous_redirected(school, client):
    r = client.get(RANGE)
    assert r.status_code == 302
    assert "login" in r["Location"]


def test_csrf_enforced(school):
    c = login("ADMIN", enforce_csrf_checks=True)
    assert post(c).status_code == 403
    assert count() == 0


# --- success -------------------------------------------------------------


def test_group_stage_fifteen_rows(school, admin):
    r = post(admin)
    assert r.status_code == 302
    assert r["Location"] == f"{MONTH}?mese=2026-10"
    rows = CalendarEntry.objects.all()
    assert len(rows) == 15
    assert all(e.group == "QUARTE" and e.is_group_stage for e in rows)
    assert all(e.date.weekday() < 5 for e in rows)
    html = page(admin, "?mese=2026-10")
    assert "Applicate 15 righe dal 05/10/2026 al 23/10/2026." in html
    assert resolve_hours(rows, d(7), 0, "QUARTE") == 0
    assert "Stage" in page(admin, "?mese=2026-10")


def test_cross_month_plain_and_htmx(school, admin):
    r = post(admin, start="2026-10-26", end="2026-11-06", kind="closure", group="")
    assert r["Location"] == f"{MONTH}?mese=2026-10"
    assert count() == 10
    r = post(admin, start="2026-11-09", end="2026-11-10", kind="closure", group="")
    assert r.status_code == 302
    r = admin.post(
        RANGE,
        {"start": "2026-11-16", "end": "2026-11-17", "kind": "closure", "group": ""},
        **HX,
    )
    assert r.status_code == 200
    assert 'id="calendar-section"' in body(r)
    assert "Applicate 2 righe dal 16/11/2026 al 17/11/2026." in body(r)


@pytest.mark.parametrize(
    "extra, single",
    [
        ({"kind": "closure", "group": ""}, {"is_closure": "on"}),
        (
            {"kind": "short", "group": "TERZE", "max_hours": "4"},
            {"group": "TERZE", "max_hours": "4"},
        ),
        (
            {"kind": "short", "group": "TERZE", "max_hours": "5"},
            {"group": "TERZE", "max_hours": "5"},
        ),
        (
            {"kind": "class_stage", "group": "", "stage_class": "CLS"},
            {"stage_class": "CLS"},
        ),
        (
            {"kind": "start_hour", "group": "", "start_hour_min": "10"},
            {"start_hour_min": "10"},
        ),
    ],
)
def test_row_shape_matches_single_date_form(school, classes, admin, extra, single):
    pk = str(classes["PRIME"].pk)
    extra = {k: (pk if v == "CLS" else v) for k, v in extra.items()}
    single = {k: (pk if v == "CLS" else v) for k, v in single.items()}
    single.setdefault("group", extra.get("group", ""))
    r = post(admin, start="2026-10-12", end="2026-10-12", **extra)
    assert r.status_code == 302
    stored = CalendarEntry.objects.get()
    stored.delete()
    form = CalendarEntryForm({"date": "2026-10-12", **single}, school=school)
    assert form.is_valid(), form.errors
    expected = form.save(commit=False)
    for f in (
        "group",
        "max_hours",
        "is_closure",
        "start_hour_min",
        "stage_class_id",
        "is_group_stage",
    ):
        assert getattr(stored, f) == getattr(expected, f), f


def test_irrelevant_fields_are_ignored(school, classes, admin):
    r = post(
        admin,
        start="2026-10-12",
        end="2026-10-12",
        kind="closure",
        group="",
        max_hours="abc",
        stage_class="abc",
        start_hour_min="99",
    )
    assert r.status_code == 302
    e = CalendarEntry.objects.get()
    assert e.is_closure and e.max_hours == 6 and e.stage_class is None


def test_one_day_and_weekend_ranges(school, admin):
    post(admin, start="2026-10-14", end="2026-10-14")
    assert count() == 1
    r = post(admin, start="2026-10-17", end="2026-10-18")
    assert r.status_code == 200 and count() == 1
    assert "lunedì a venerdì" in body(r)
    post(admin, start="2026-10-16", end="2026-10-20", group="PRIME")
    assert sorted(e.date.day for e in CalendarEntry.objects.filter(group="PRIME")) == [
        16,
        19,
        20,
    ]


# --- validation ----------------------------------------------------------


@pytest.mark.parametrize(
    "extra",
    [
        {"start": ""},
        {"end": ""},
        {"start": "boh"},
        {"end": "2026-13-45"},
        {"start": "2026-10-20", "end": "2026-10-10"},
        {"start": "2026-08-31"},
        {"end": "2027-07-01"},
        {"kind": "short", "max_hours": "6"},
        {"kind": "short", "max_hours": "abc"},
        {"kind": "short", "max_hours": ""},
        {"group": "XX"},
        {"group": ""},
        {"kind": "class_stage", "stage_class": "abc", "group": ""},
        {"kind": "class_stage", "stage_class": "", "group": ""},
        {"kind": "class_stage", "stage_class": "CLS", "group": "PRIME"},
        {"kind": "start_hour", "start_hour_min": "7"},
        {"kind": "start_hour", "start_hour_min": "14"},
        {"kind": "start_hour", "start_hour_min": ""},
        {"kind": ""},
        {"kind": "boh"},
    ],
)
def test_rejected_inputs(school, classes, admin, extra):
    other = Class.objects.create(
        school=make_school("Altra"),
        name="9Z",
        group="PRIME",
    )
    extra = dict(extra)
    if extra.get("stage_class") == "CLS":
        extra["stage_class"] = str(classes["PRIME"].pk)
    r = admin.post(
        RANGE,
        {
            "start": "2026-10-05",
            "end": "2026-10-09",
            "group": "QUARTE",
            "kind": "group_stage",
            **extra,
        },
        **HX,
    )
    assert r.status_code == 200
    assert r["HX-Retarget"] == "#calendar-form-area"
    assert count() == 0
    html = body(r)
    assert 'aria-invalid="true"' in html
    for ref in re.findall(r'aria-describedby="([^"]*)"', html):
        for ident in ref.split():
            assert f'id="{ident}"' in html
    assert other.pk


def test_class_of_another_school_rejected(school, admin):
    from test_calendar_screens import make_school

    other = Class.objects.create(school=make_school("Altra"), name="9Z", group="PRIME")
    r = post(admin, kind="class_stage", group="", stage_class=str(other.pk))
    assert r.status_code == 200 and count() == 0


# --- conflicts -----------------------------------------------------------


def test_conflict_reports_date_and_link(school, admin):
    existing = entry(school, 14, group="QUARTE")
    r = post(admin, kind="short", max_hours="4")
    assert r.status_code == 200
    assert count() == 1
    html = body(r)
    assert "14/10/2026" in html
    assert f"/dati-scuola/calendario/{existing.pk}/modifica/" in html


def test_whole_school_and_class_rows_do_not_conflict(school, classes, admin):
    entry(school, 14, group=None, is_closure=True)
    entry(school, 15, stage_class=classes["PRIME"], group=None)
    r = post(admin, kind="short", max_hours="4")
    assert r.status_code == 302
    assert count() == 2 + 15


def test_class_stage_next_to_group_row(school, classes, admin):
    entry(school, 14, group="PRIME")
    r = post(
        admin,
        kind="class_stage",
        group="",
        stage_class=str(classes["PRIME"].pk),
        start="2026-10-14",
        end="2026-10-14",
    )
    assert r.status_code == 302


def test_twelve_conflicts_listed_as_ten_plus_two(school, admin):
    for day in (5, 6, 7, 8, 9, 12, 13, 14, 15, 16, 19, 20):
        entry(school, day, group="QUARTE")
    n = count()
    assert n == 12
    r = post(admin, start="2026-10-05", end="2026-10-23")
    html = body(r)
    assert "e altre 2" in html
    assert count() == n
    assert "16/10/2026" in html
    assert "19/10/2026" not in html


def test_atomic_on_integrity_error(school, admin):
    with mock.patch(
        "school.forms.CalendarEntry.objects.bulk_create",
        side_effect=IntegrityError("boom"),
    ):
        r = post(admin)
    assert r.status_code == 200
    assert count() == 0


def test_atomic_when_a_row_fails_midway(school, admin):
    real = CalendarEntry.objects.bulk_create

    def failing(rows, *a, **kw):
        real(rows[:3], *a, **kw)
        raise IntegrityError("boom")

    with mock.patch("school.forms.CalendarEntry.objects.bulk_create", failing):
        r = post(admin)
    assert r.status_code == 200
    assert count() == 0


def test_query_count_is_constant(school, admin):
    def run(start, end, group):
        with CaptureQueriesContext(connection) as ctx:
            r = post(admin, start=start, end=end, group=group)
        assert r.status_code == 302
        return len(ctx)

    short = run("2026-10-05", "2026-10-09", "PRIME")
    long = run("2026-11-02", "2026-12-11", "SECONDE")
    assert CalendarEntry.objects.filter(group="SECONDE").count() == 30
    assert short == long


# --- rendering -----------------------------------------------------------


def test_script_class_name_escaped(school, admin):
    c = Class.objects.create(
        school=school, name="<script>alert(1)</script>", group="PRIME"
    )
    html = admin.get(RANGE).content.decode()
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
    r = admin.post(
        RANGE,
        {
            "start": "2026-10-12",
            "end": "2026-10-12",
            "kind": "class_stage",
            "group": "",
            "stage_class": str(c.pk),
        },
        follow=True,
    )
    assert "<script>alert(1)</script>" not in r.content.decode()
    assert "&lt;script&gt;" in r.content.decode()
