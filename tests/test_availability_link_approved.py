import datetime
import json
from html.parser import HTMLParser

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from accounts.models import User
from availability import grid as g
from availability import links
from availability.models import Availability, AvailabilityLink, LinkLookupFailure
from scheduling.models import Timetable, TimetableState
from scheduling.services import approve_timetable, undo_approval
from school.models import School, Teacher

D = datetime.date
W1, W2 = D(2026, 9, 7), D(2026, 9, 14)
T0 = datetime.datetime(2026, 10, 1, 12, 0, tzinfo=datetime.UTC)
HX = {"HTTP_HX_REQUEST": "true"}
NOTICE = (
    "Orario gia approvato: la disponibilita di questa settimana non si puo "
    "piu modificare. Per cambiarla rivolgiti alla segreteria."
)
REFUSAL = "La settimana e gia approvata: la disponibilita non e stata modificata."
CREATE_REFUSAL = "Settimana gia approvata: non si possono creare link."


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
    monkeypatch.setattr(links, "now", lambda: T0)


@pytest.fixture
def user(db):
    return User.objects.create_user("a@example.com", "correct-horse", "ADMIN")


def approved(school, week=W1):
    return Timetable.objects.create(
        school=school, week_start=week, state=TimetableState.APPROVATO
    )


def all_off():
    grid = g.default_grid()
    for day in grid.values():
        for slot in day:
            slot["disponibile"] = False
    return grid


def post(client, token, grid):
    payload = grid if isinstance(grid, str) else json.dumps(grid)
    return client.post(f"/l/{token}/salva/", {"grid": payload}, **HX)


def test_get_read_only(client, school, teacher, clock):
    link, token = links.create_link(teacher, W1, None)
    approved(school)
    r = client.get(f"/l/{token}/")
    body = r.content.decode()
    assert r.status_code == 200
    assert NOTICE in body
    assert "ROSSI" in body
    assert "07/09/2026" in body
    assert "11/09/2026" in body
    assert "Salva" not in body
    assert "hx-post" not in body
    assert "@click" not in body
    assert "no-store" in r["Cache-Control"]
    assert r["Referrer-Policy"] == "no-referrer"
    link.refresh_from_db()
    assert link.used_at is not None


@pytest.mark.parametrize("state", [None, TimetableState.BOZZA])
def test_not_approved_page_unchanged(client, school, teacher, clock, state):
    if state:
        Timetable.objects.create(school=school, week_start=W1, state=state)
    _, token = links.create_link(teacher, W1, None)
    body = client.get(f"/l/{token}/").content.decode()
    assert "Salva" in body
    assert "@click" in body
    assert NOTICE not in body


@pytest.mark.parametrize("previous", [False, True])
@pytest.mark.parametrize("grid", ["valid", "invalid"])
def test_post_refused_409(client, school, teacher, clock, previous, grid):
    _, token = links.create_link(teacher, W1, None)
    approved(school)
    if previous:
        Availability.objects.create(
            school=school, teacher=teacher, week_start=W1, slots=g.default_grid()
        )
    r = post(client, token, all_off() if grid == "valid" else "not json")
    assert r.status_code == 409
    assert REFUSAL in r.content.decode()
    assert token not in r.content.decode()
    if previous:
        assert Availability.objects.get().slots == g.default_grid()
    else:
        assert Availability.objects.count() == 0


def test_refusals_do_not_rate_limit(client, school, teacher, clock):
    _, token = links.create_link(teacher, W1, None)
    approved(school)
    for _ in range(30):
        assert post(client, token, all_off()).status_code == 409
    assert LinkLookupFailure.objects.count() == 0
    assert not links.is_rate_limited("127.0.0.1")


def test_undo_approval_makes_link_editable(client, school, teacher, clock, user):
    tt = Timetable.objects.create(school=school, week_start=W1)
    _, token = links.create_link(teacher, W1, None)
    approve_timetable(tt.pk, user)
    assert post(client, token, all_off()).status_code == 409
    undo_approval(tt.pk, user, "errore")
    assert "Salva" in client.get(f"/l/{token}/").content.decode()
    assert post(client, token, all_off()).status_code == 200
    assert Availability.objects.get().slots == all_off()


def test_approval_leaves_links_untouched(school, teacher, clock, user):
    tt = Timetable.objects.create(school=school, week_start=W1)
    links.create_link(teacher, W1, None)
    before = list(AvailabilityLink.objects.values())
    approve_timetable(tt.pk, user)
    assert list(AvailabilityLink.objects.values()) == before


def test_other_week_not_blocked(client, school, teacher, clock):
    approved(school, W2)
    _, token = links.create_link(teacher, W1, None)
    assert "Salva" in client.get(f"/l/{token}/").content.decode()
    assert post(client, token, all_off()).status_code == 200


def test_save_locks_timetable_row(client, school, teacher, clock):
    Timetable.objects.create(school=school, week_start=W1)
    _, token = links.create_link(teacher, W1, None)
    with CaptureQueriesContext(connection) as ctx:
        assert post(client, token, all_off()).status_code == 200
    locks = [
        q["sql"]
        for q in ctx.captured_queries
        if "scheduling_timetable" in q["sql"] and "FOR UPDATE" in q["sql"]
    ]
    assert locks


def test_create_link_refused_for_approved_week(school, teacher, clock):
    approved(school)
    with pytest.raises(links.LinkError, match=CREATE_REFUSAL):
        links.create_link(teacher, W1, None)
    assert AvailabilityLink.objects.count() == 0


def test_create_keeps_existing_link_and_generate_400(school, teacher, clock, user):
    tt = Timetable.objects.create(school=school, week_start=W1)
    link, _ = links.create_link(teacher, W1, None)
    approve_timetable(tt.pk, user)
    admin = Client()
    admin.force_login(user)
    r = admin.post(f"/disponibilita/{W1:%Y-%m-%d}/docente/{teacher.pk}/link/")
    assert r.status_code == 400
    assert CREATE_REFUSAL in r.content.decode()
    link.refresh_from_db()
    assert link.revoked_at is None
    assert AvailabilityLink.objects.count() == 1


def test_admin_screen_still_edits_approved_week(school, teacher, user):
    approved(school)
    admin = Client()
    admin.force_login(user)
    r = admin.post(
        f"/disponibilita/{W1:%Y-%m-%d}/docente/{teacher.pk}/salva/",
        {"grid": json.dumps(all_off())},
        **HX,
    )
    assert r.status_code == 200
    assert Availability.objects.get().slots == all_off()


class _Rows(HTMLParser):
    """Direct children (tag, classes, text) of each .av-public-row."""

    VOID = {"input", "br", "img", "hr"}

    def __init__(self):
        super().__init__()
        self.rows = []
        self.stack = []
        self.depth = None

    def handle_starttag(self, tag, attrs):
        classes = (dict(attrs).get("class") or "").split()
        if self.depth is not None and len(self.stack) == self.depth + 1:
            self.rows[-1][1].append(
                {"classes": classes, "text": "", "attrs": dict(attrs)}
            )
        if tag in self.VOID:
            return
        self.stack.append(tag)
        if "av-public-row" in classes and self.depth is None:
            self.depth = len(self.stack) - 1
            self.rows.append((classes, []))

    def handle_endtag(self, tag):
        if tag in self.VOID:
            return
        self.stack.pop()
        if self.depth is not None and len(self.stack) == self.depth:
            self.depth = None

    def handle_data(self, data):
        if self.depth is not None and self.rows[-1][1]:
            self.rows[-1][1][-1]["text"] += data


def _rows(body):
    parser = _Rows()
    parser.feed(body)
    heads = [kids for classes, kids in parser.rows if "av-public-head" in classes]
    days = [kids for classes, kids in parser.rows if "av-public-head" not in classes]
    assert len(heads) == 1
    return heads[0], days


def _assert_head_aligned(body):
    hours = [s["ora_inizio"] for s in g.default_grid()["lunedi"]]
    head, days = _rows(body)
    assert len(head) == 1 + len(hours) == 7
    assert "av-public-corner" in head[0]["classes"]
    assert "Giorno" in head[0]["text"]
    assert [c["text"].strip() for c in head[1:]] == hours
    assert not [c for c in head if "visually-hidden" in c["classes"]]
    assert len(days) == 5
    assert all(len(d) == len(head) for d in days)
    return head, days


def test_read_only_hour_headers_sit_above_their_columns(client, school, teacher, clock):
    _, token = links.create_link(teacher, W1, None)
    approved(school)
    body = client.get(f"/l/{token}/").content.decode()
    _assert_head_aligned(body)
