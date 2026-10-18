import datetime
import json
import logging
from html.parser import HTMLParser

import pytest
from django.test import Client

from accounts.models import User
from availability import grid as g
from availability import links
from availability.models import Availability, AvailabilityLink, LinkLookupFailure
from school.models import School, Teacher

D = datetime.date
W1, W2 = D(2026, 9, 7), D(2026, 9, 14)
T0 = datetime.datetime(2026, 10, 1, 12, 0, tzinfo=datetime.UTC)
HX = {"HTTP_HX_REQUEST": "true"}
NEUTRAL = "Link non valido o scaduto, chiedi un nuovo link alla segreteria"


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


def page(token):
    return f"/l/{token}/"


def save_url(token):
    return f"/l/{token}/salva/"


def post(client, token, grid, **extra):
    payload = grid if isinstance(grid, str) else json.dumps(grid)
    return client.post(save_url(token), {"grid": payload}, **HX, **extra)


def all_off():
    grid = g.default_grid()
    for day in grid.values():
        for slot in day:
            slot["disponibile"] = False
    return grid


def stored(teacher, week=W1):
    return Availability.objects.filter(teacher=teacher, week_start=week).first()


def test_valid_link_shows_name_week_and_grid(client, teacher, clock):
    _, token = links.create_link(teacher, W1, None)
    r = client.get(page(token))
    body = r.content.decode()
    assert r.status_code == 200
    assert "ROSSI" in body and "07/09/2026" in body and "11/09/2026" in body
    assert body.count('class="av-pcell') == 30
    assert body.count('aria-pressed="true"') == 30
    assert "Salva" in body
    assert "av-link-form" in body and "csrfmiddlewaretoken" not in body


def test_prefilled_from_stored_and_inherited(client, teacher, clock):
    grid = g.default_grid()
    grid["lunedi"][0]["disponibile"] = False
    Availability.objects.create(
        school=teacher.school, teacher=teacher, week_start=W1, slots=grid
    )
    _, token = links.create_link(teacher, W2, None)  # inherits W1
    body = client.get(page(token)).content.decode()
    assert body.count('aria-pressed="false"') == 1
    assert body.count('aria-pressed="true"') == 29
    assert "&#10007; No" in body or "✗ No" in body


def test_cells_have_text_marker_and_size_css():
    css = open("static/css/app.css").read()
    assert "min-width: 44px; min-height: 44px" in css
    assert "grid-template-columns: 3.4rem repeat(6, minmax(0, 1fr))" in css


def test_headers_and_no_cookies_or_third_party(client, teacher, clock):
    _, token = links.create_link(teacher, W1, None)
    for r in (
        client.get(page(token)),
        post(client, token, g.default_grid()),
        client.get(page("A" * 43)),
    ):
        assert "no-store" in r["Cache-Control"]
        assert r["Referrer-Policy"] == "no-referrer"
        assert not r.cookies
    body = client.get(page(token)).content.decode()
    assert "http://" not in body and "https://" not in body
    assert "csrf" not in body.lower()


def test_save_stores_and_admin_screen_matches(client, teacher, clock):
    _, token = links.create_link(teacher, W1, None)
    grid = g.default_grid()
    grid["martedi"][3]["disponibile"] = False
    r = post(client, token, grid)
    assert r.status_code == 200
    assert "Disponibilita salvata" in r.content.decode()
    assert stored(teacher).slots == grid
    admin = Client()
    admin.force_login(User.objects.create_user("a@example.com", "pw-correct", "ADMIN"))
    body = admin.get(f"/disponibilita/?settimana={W1}").content.decode()
    assert "Salvata per questa settimana" in body
    assert body.count('aria-pressed="false"') == 1


def test_admin_write_visible_on_page(client, teacher, clock):
    _, token = links.create_link(teacher, W1, None)
    grid = all_off()
    Availability.objects.create(
        school=teacher.school, teacher=teacher, week_start=W1, slots=grid
    )
    body = client.get(page(token)).content.decode()
    assert body.count('aria-pressed="false"') == 30


def test_last_write_wins(client, teacher, clock):
    _, token = links.create_link(teacher, W1, None)
    post(client, token, all_off())
    post(client, token, g.default_grid())
    assert stored(teacher).slots == g.default_grid()


def test_request_cannot_choose_teacher_or_week(client, school, teacher, clock):
    other = Teacher.objects.create(school=school, name="BIANCHI", type="CONTRATTO")
    keep = g.default_grid()
    Availability.objects.create(school=school, teacher=other, week_start=W1, slots=keep)
    _, token = links.create_link(teacher, W1, None)
    r = client.post(
        save_url(token),
        {
            "grid": json.dumps(all_off()),
            "teacher": other.pk,
            "teacher_id": other.pk,
            "week": str(W2),
            "week_start": str(W2),
        },
        **HX,
    )
    assert r.status_code == 200
    assert stored(other).slots == keep
    assert stored(teacher, W2) is None
    assert stored(teacher, W1).slots == all_off()
    assert Availability.objects.count() == 2


def _bad_grids():
    missing = g.default_grid()
    del missing["lunedi"]
    empty = g.default_grid()
    empty["lunedi"] = []
    badtime = g.default_grid()
    badtime["lunedi"][0]["ora_inizio"] = "25:99"
    nonbool = g.default_grid()
    nonbool["lunedi"][0]["disponibile"] = "si"
    return [missing, empty, badtime, nonbool, "not json", "[]", ""]


@pytest.mark.parametrize("bad", _bad_grids())
def test_malformed_rejected_nothing_stored(client, teacher, clock, bad):
    _, token = links.create_link(teacher, W1, None)
    r = post(client, token, bad)
    assert r.status_code == 400
    assert "nulla e stato salvato" in r.content.decode()
    assert Availability.objects.count() == 0


def test_all_unavailable_accepted(client, teacher, clock):
    _, token = links.create_link(teacher, W1, None)
    assert post(client, token, all_off()).status_code == 200
    assert stored(teacher).slots == all_off()


def test_neutral_pages_identical(client, teacher, clock):
    link, good = links.create_link(teacher, W1, None)
    _, revoked = links.create_link(teacher, W2, None)
    AvailabilityLink.objects.filter(week_start=W2).update(revoked_at=T0)
    _, expired = links.create_link(
        Teacher.objects.create(school=teacher.school, name="VERDI", type="CONTRATTO"),
        W1,
        None,
    )
    AvailabilityLink.objects.filter(teacher__name="VERDI").update(
        expires_at=T0 - datetime.timedelta(seconds=1)
    )
    responses = [
        client.get(page(t), REMOTE_ADDR=f"10.0.0.{i}")
        for i, t in enumerate(["A" * 43, "short", revoked, expired])
    ]
    first = responses[0]
    assert first.status_code == 404
    assert NEUTRAL in first.content.decode()
    for r in responses:
        assert r.status_code == 404
        assert r.content == first.content
        assert "ROSSI" not in r.content.decode()


def test_inactive_teacher_neutral(client, teacher, clock):
    _, token = links.create_link(teacher, W1, None)
    Teacher.objects.filter(pk=teacher.pk).update(active=False)
    r = client.get(page(token))
    assert r.status_code == 404 and NEUTRAL in r.content.decode()


def test_revoked_or_expired_between_load_and_save(client, teacher, clock):
    link, token = links.create_link(teacher, W1, None)
    assert client.get(page(token)).status_code == 200
    links.revoke_link(link.pk)
    r = post(client, token, all_off())
    assert r.status_code == 404 and NEUTRAL in r.content.decode()
    assert Availability.objects.count() == 0
    link2, token2 = links.create_link(teacher, W1, None)
    clock["now"] = link2.expires_at + datetime.timedelta(seconds=1)
    r = post(client, token2, all_off())
    assert r.status_code == 404 and NEUTRAL in r.content.decode()
    assert Availability.objects.count() == 0


def test_rate_limit_on_page_and_save(client, teacher, clock):
    _, good = links.create_link(teacher, W1, None)
    for _ in range(10):
        assert client.get(page("A" * 43)).status_code == 404
    assert client.get(page("A" * 43)).status_code == 429
    assert client.get(page(good)).status_code == 429
    assert post(client, good, all_off()).status_code == 429
    assert Availability.objects.count() == 0


def test_save_failures_count_towards_limit(client, teacher, clock):
    for _ in range(10):
        assert post(client, "A" * 43, all_off()).status_code == 404
    assert post(client, "A" * 43, all_off()).status_code == 429


def test_forged_cross_site_post_fails(teacher, clock):
    _, token = links.create_link(teacher, W1, None)
    c = Client(enforce_csrf_checks=True)
    body = {"grid": json.dumps(all_off())}
    assert c.post(save_url(token), body).status_code == 403  # plain form post
    r = c.post(save_url(token), body, **HX, HTTP_ORIGIN="https://evil.example")
    assert r.status_code == 403
    r = c.post(save_url(token), body, **HX, HTTP_SEC_FETCH_SITE="cross-site")
    assert r.status_code == 403
    assert Availability.objects.count() == 0
    r = c.post(save_url(token), body, **HX, HTTP_ORIGIN="http://testserver")
    assert r.status_code == 200


def test_used_at_first_open_only(client, teacher, clock):
    link, token = links.create_link(teacher, W1, None)
    client.get(page(token))
    link.refresh_from_db()
    assert link.used_at == T0
    clock["now"] = T0 + datetime.timedelta(hours=1)
    assert client.get(page(token)).status_code == 200
    link.refresh_from_db()
    assert link.used_at == T0


def test_grants_nothing_else(client, teacher, clock):
    _, token = links.create_link(teacher, W1, None)
    client.get(page(token))
    post(client, token, all_off())
    for url in (
        "/disponibilita/",
        "/disponibilita/link/",
        f"/disponibilita/{W1}/docente/{teacher.pk}/salva/",
        f"/disponibilita/{W1}/docente/{teacher.pk}/toggle/",
        "/orario/",
    ):
        r = client.get(url + f"?t={token}", HTTP_AUTHORIZATION=f"Bearer {token}")
        assert r.status_code in (302, 405) and "login" in r.get("Location", "login")
    r = client.post(f"/disponibilita/{W1}/docente/{teacher.pk}/salva/", {})
    assert r.status_code == 302


def test_assunto_teacher_saves(client, school, clock):
    t = Teacher.objects.create(school=school, name="NERI", type="ASSUNTO")
    _, token = links.create_link(t, W1, None)
    body = client.get(page(token)).content.decode()
    assert "NERI" in body
    assert post(client, token, all_off()).status_code == 200
    assert stored(t).slots == all_off()


def test_token_never_logged(client, teacher, clock, caplog):
    caplog.set_level(logging.DEBUG)
    logging.getLogger("django.request").setLevel(logging.DEBUG)
    _, token = links.create_link(teacher, W1, None)
    unknown = "B" * 43
    assert client.get(page(token)).status_code == 200
    assert post(client, token, all_off()).status_code == 200
    assert post(client, token, "bad").status_code == 400
    assert client.get(page(unknown)).status_code == 404
    assert post(client, unknown, all_off()).status_code == 404
    for _ in range(11):
        client.get(page(unknown))
    assert client.get(page(unknown)).status_code == 429
    assert caplog.records
    for secret in (token, unknown):
        for record in caplog.records:
            assert secret not in record.getMessage()
            assert secret not in str(record.__dict__.get("args"))
            assert secret not in str(record.__dict__.get("msg"))


@pytest.mark.parametrize("path", ["/l/a/b/", "/l/a%2Fb/", "/l/a/b/c/salva/"])
def test_slash_tokens_get_neutral_page_and_accounting(client, teacher, clock, path):
    _, good = links.create_link(teacher, W1, None)
    reference = client.get(page("A" * 43), REMOTE_ADDR="10.9.9.9")
    r = client.get(path)
    assert r.status_code == 404
    assert r.content == reference.content
    assert "no-store" in r["Cache-Control"]
    assert r["Referrer-Policy"] == "no-referrer"
    assert not r.cookies
    assert LinkLookupFailure.objects.filter(client_address="127.0.0.1").count() == 1
    for _ in range(9):
        client.get(path)
    assert client.get(path).status_code == 429
    assert client.get(page(good)).status_code == 429


def test_slash_token_post_and_logging(client, teacher, clock, caplog):
    caplog.set_level(logging.DEBUG)
    logging.getLogger("django.request").setLevel(logging.DEBUG)
    assert post(client, "abcdefghij/klmnop", all_off()).status_code == 404
    assert client.get("/l/abcdefghij/klmnop/").status_code == 404
    for record in caplog.records:
        assert "klmnop" not in record.getMessage()
        assert "ghij" not in record.getMessage()


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


def test_hour_headers_sit_above_their_columns(client, teacher, clock):
    _, token = links.create_link(teacher, W1, None)
    body = client.get(page(token)).content.decode()
    head, days = _assert_head_aligned(body)
    for day in days:
        for header, cell in zip(head[1:], day[1:], strict=True):
            assert cell["attrs"]["aria-label"].endswith(header["text"].strip())
