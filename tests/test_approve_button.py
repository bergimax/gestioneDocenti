import datetime

import pytest
from django.test import Client

from accounts.models import User
from ledger.models import LedgerEntry
from scheduling.models import Slot, Timetable, TimetableState
from school.models import Assignment

D = datetime.date
MON = D(2026, 10, 12)
WEEK = f"{MON:%Y-%m-%d}"
URL = f"/orario/{WEEK}/approva/"
PAGE = f"/orario/?settimana={WEEK}"
GRID = f"/orario/{WEEK}/griglia/"


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    from scheduling import views

    monkeypatch.setattr(views, "today", lambda: D(2026, 10, 14))


@pytest.fixture
def admin(db):
    c = Client()
    c.force_login(
        User.objects.create_user("boss@example.com", "pw-long-enough", "ADMIN")
    )
    return c


def text(r):
    return r.content.decode()


def test_button_on_page_and_grid(w, admin):
    for path in (PAGE, GRID):
        html = text(admin.get(path))
        assert "Approva settimana" in html
        assert f'hx-post="{URL}"' in html and f'action="{URL}"' in html
        assert 'hx-target="#timetable-body"' in html
        assert 'hx-confirm="Approvare la settimana?' in html


def test_button_absent_for_secretary(w, seg):
    for path in (PAGE, GRID):
        assert "Approva settimana" not in text(seg.get(path))


def test_button_absent_when_approved_or_no_timetable(w, admin):
    admin.post(URL)
    for path in (PAGE, GRID):
        assert "Approva settimana" not in text(admin.get(path))
    Timetable.objects.all().update(state=TimetableState.BOZZA)
    r = admin.get("/orario/?settimana=2026-10-19")
    assert r.status_code == 200 and "Approva settimana" not in text(r)


def test_post_shows_numbers_outside_body(w, admin):
    Assignment.objects.filter(pk=w.asg["a_mat"].pk).delete()
    r = admin.post(URL)
    html = text(r)
    assert r.status_code == 200
    w.tt.refresh_from_db()
    assert w.tt.state == TimetableState.APPROVATO
    assert "Approvata" in html and "data-movable" not in html
    assert "tt-assign" not in html
    for label, value in (
        ("Ore scalate", 6),
        ("Assegnazioni aggiornate", 3),
        ("Lezioni senza monte ore", 2),
    ):
        assert f"<dt>{label}</dt><dd>{value}</dd>" in html
    assert html.index("Ore scalate") > html.index('id="tt-approval-result"')
    assert (
        'id="tt-approval-result" role="status" aria-live="polite" hx-swap-oob="true"'
        in html
    )
    grid = text(admin.get(GRID))
    assert "Ore scalate" not in grid and "tt-approval-result" not in grid
    assert "Annulla approvazione" in html


def test_second_post_is_noop(w, admin):
    admin.post(URL)
    w.tt.refresh_from_db()
    version, count = w.tt.version, LedgerEntry.objects.count()
    r = admin.post(URL)
    w.tt.refresh_from_db()
    assert r.status_code == 200 and "Ore scalate" not in text(r)
    assert w.tt.version == version and LedgerEntry.objects.count() == count


def test_panels_refreshed_out_of_band(w, admin):
    html = text(admin.post(URL))
    assert 'id="job-area"' in html and "Genera orario" not in html
    for panel in ("tt-deroga-panel", "tt-instruction-panel", "tt-repair-panel"):
        assert f'id="{panel}"' in html
    assert html.count('hx-swap-oob="true"') >= 5
    assert "<summary>Deroga</summary>" not in html
    assert "<summary>Sostituzione</summary>" not in html
    page = text(admin.get(PAGE))
    assert "Genera orario" not in page and "<summary>Deroga</summary>" not in page


def test_empty_week_offers_approva_comunque(w, admin):
    Slot.objects.all().delete()
    r = admin.post(URL)
    html = text(r)
    assert r.status_code == 409
    assert "Nessuna lezione: approvare comunque?" in html
    assert 'name="confirm" value="1"' in html and "Approva comunque" in html
    assert admin.post(URL, {"confirm": "1"}).status_code == 200


def test_conflicts_offer_approva_comunque(w, admin):
    Timetable.objects.filter(pk=w.tt.pk).update(
        conflicts=[{"chiave": "k1", "tipo": "X"}]
    )
    html = text(admin.post(URL))
    assert "1 conflitti non risolti" in html
    assert 'name="confirm" value="1"' in html
    assert not LedgerEntry.objects.exists()


def test_undo_clears_figures(w, admin):
    admin.post(URL)
    html = text(
        admin.post(f"/orario/{WEEK}/annulla-approvazione/", {"motivo": "errore"})
    )
    assert 'id="tt-approval-result"' in html and 'hx-swap-oob="true"' in html
