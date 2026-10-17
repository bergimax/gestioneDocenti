"""Free-text notes on a timetable (#49): model, service, endpoint, panel."""

import datetime
import re
from pathlib import Path
from unittest import mock

import pytest
from django.db import connection

from projects import permissions
from scheduling import audit, feedback, proposals, views
from scheduling.models import (
    AuditLog,
    SoftWeightAdjustment,
    Timetable,
    TimetableFeedback,
    TimetableState,
)
from scheduling.services import approve_timetable

D = datetime.date
MON = D(2026, 10, 12)
NEXT = D(2026, 10, 19)
URL = "/orario/2026-10-12/nota/"


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    monkeypatch.setattr(views, "today", lambda: D(2026, 10, 14))


def user_of(client):
    from accounts.models import User

    return User.objects.get(pk=client.session["_auth_user_id"])


def post(client, text, url=URL):
    return client.post(url, {"nota": text})


def page(client):
    return client.get("/orario/?settimana=2026-10-12").content.decode()


# ---- saving ---------------------------------------------------------------


def test_admin_saves_a_trimmed_note(w, admin):
    r = post(admin, "  Troppi buchi il martedì  ")
    assert r.status_code == 200
    html = r.content.decode()
    assert "Nota salvata." in html
    assert "Troppi buchi il martedì" in html
    row = TimetableFeedback.objects.get()
    assert row.note == "Troppi buchi il martedì"
    assert (row.school_id, row.timetable_id, row.week_start) == (
        w.school.pk,
        w.tt.pk,
        MON,
    )
    assert row.user == user_of(admin)


def test_snapshot_matches_draft_and_approved_and_stays(w, admin):
    Timetable.objects.filter(pk=w.tt.pk).update(
        quality_score=72.5, quality_level="B", soft_conflicts=3
    )
    post(admin, "bozza")
    approve_timetable(w.tt.pk, user_of(admin))
    post(admin, "approvato")
    approved, draft = TimetableFeedback.objects.all()
    assert (draft.quality_score, draft.quality_level, draft.soft_conflicts) == (
        72.5,
        "B",
        3,
    )
    assert draft.timetable_state == TimetableState.BOZZA
    assert approved.timetable_state == TimetableState.APPROVATO
    assert approved.quality_score == 72.5
    Timetable.objects.filter(pk=w.tt.pk).update(quality_score=10, soft_conflicts=9)
    draft.refresh_from_db()
    assert (draft.quality_score, draft.soft_conflicts) == (72.5, 3)


def test_unsolved_timetable_saves_nulls(w, admin):
    post(admin, "mai risolto")
    row = TimetableFeedback.objects.get()
    assert (row.quality_score, row.quality_level) == (None, None)
    assert "Qualità non calcolata" in page(admin)


def test_quality_line_shows_level_and_score(w, admin):
    Timetable.objects.filter(pk=w.tt.pk).update(quality_score=72.5, quality_level="B")
    post(admin, "x")
    assert re.search(r"Qualità: livello B \(punteggio 72[.,]5\)", page(admin))


def test_week_without_slots_accepts_a_note(w, admin):
    Timetable.objects.create(school=w.school, week_start=NEXT)
    assert post(admin, "vuota", "/orario/2026-10-19/nota/").status_code == 200
    assert TimetableFeedback.objects.get().week_start == NEXT


def test_every_post_creates_a_row(w, admin):
    post(admin, "uguale")
    post(admin, "uguale")
    assert TimetableFeedback.objects.count() == 2


# ---- validation -----------------------------------------------------------


@pytest.mark.parametrize("text", ["", "   \n\t "])
def test_empty_or_blank_is_rejected(w, admin, text):
    r = post(admin, text)
    assert r.status_code == 400
    assert 'role="alert"' in r.content.decode()
    assert "field has-error" in r.content.decode()
    assert not TimetableFeedback.objects.exists()


def test_rejected_note_keeps_typed_text(w, admin):
    r = post(admin, "x" * 2001)
    assert r.status_code == 400
    assert "x" * 2001 in r.content.decode()
    assert not TimetableFeedback.objects.exists()


def test_2000_characters_accepted_counted_as_characters(w, admin):
    text = "è" * 2000  # 4000 bytes
    assert post(admin, text).status_code == 200
    assert TimetableFeedback.objects.get().note == text
    assert post(admin, "è" * 2001).status_code == 400
    assert TimetableFeedback.objects.count() == 1


# ---- access ---------------------------------------------------------------


def test_segreteria_gets_403_and_nothing_written(w, seg):
    assert post(seg, "no").status_code == 403
    assert not TimetableFeedback.objects.exists()
    assert not AuditLog.objects.filter(action=audit.FEEDBACK_ADDED).exists()


def test_anonymous_is_sent_to_login(w, client):
    r = post(client, "no")
    assert r.status_code == 302 and "login" in r["Location"]
    assert not TimetableFeedback.objects.exists()


def test_get_is_405(w, admin):
    assert admin.get(URL).status_code == 405


def test_segreteria_page_has_no_panel_or_notes(w, admin, seg):
    post(admin, "segreta-nota-xyz")
    html = page(seg)
    for text in ("Nota sull'orario", "Salva nota", "tt-feedback-panel"):
        assert text not in html
    assert "segreta-nota-xyz" not in html
    assert "tt-feedback-panel" in page(admin)


def test_panel_is_outside_timetable_body(w, admin):
    html = page(admin)
    body = html.index('id="timetable-body"')
    panel = html.index('id="tt-feedback-panel"')
    # the panel opens after the body block has closed
    depth = 0
    for tag in re.finditer(r"<(/?)div\b", html[body:panel]):
        depth += -1 if tag.group(1) else 1
    assert depth <= 0


def test_notes_of_the_viewed_week_only_newest_first(w, admin):
    Timetable.objects.create(school=w.school, week_start=NEXT)
    post(admin, "prima")
    post(admin, "seconda")
    post(admin, "altra settimana", "/orario/2026-10-19/nota/")
    html = page(admin)
    assert html.index("seconda") < html.index("prima")
    assert "altra settimana" not in html


def test_empty_state(w, admin):
    assert "Nessuna nota per questa settimana." in page(admin)


def test_note_is_escaped_and_keeps_line_breaks(w, admin):
    post(admin, "<script>alert(1)</script>\nsecondo rigo")
    html = page(admin)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;\nsecondo rigo" in html
    assert "white-space: pre-line" in Path("static/css/app.css").read_text()


def test_unknown_week_or_missing_timetable_is_404(w, admin):
    assert post(admin, "x", "/orario/pippo/nota/").status_code == 404
    assert post(admin, "x", "/orario/2026-10-13/nota/").status_code == 404
    r = post(admin, "x", "/orario/2026-10-19/nota/")  # listed, no timetable
    assert r.status_code == 404
    assert not Timetable.objects.filter(week_start=NEXT).exists()
    assert not TimetableFeedback.objects.exists()


def test_no_panel_for_a_week_without_timetable(w, admin):
    html = admin.get("/orario/?settimana=2026-10-19").content.decode()
    for text in ("tt-feedback-panel", "Salva nota", "Nota sull'orario"):
        assert text not in html


# ---- append-only ------------------------------------------------------------


def test_append_only(w, admin):
    post(admin, "fissa")
    row = TimetableFeedback.objects.get()
    row.note = "cambiata"
    with pytest.raises(NotImplementedError):
        row.save()
    with pytest.raises(NotImplementedError):
        TimetableFeedback.objects.update(note="x")
    with pytest.raises(NotImplementedError):
        TimetableFeedback.objects.all().delete()
    with pytest.raises(NotImplementedError):
        row.delete()
    assert TimetableFeedback.objects.get().note == "fissa"


def test_no_edit_or_delete_url():
    from django.urls import get_resolver

    names = {n for n in get_resolver().reverse_dict if isinstance(n, str)}
    assert {n for n in names if "feedback" in n} == {"timetable_feedback"}


def test_model_has_no_vote_or_reason_fields():
    names = {f.name for f in TimetableFeedback._meta.get_fields()}
    assert not {"voto", "motivi"} & names


def test_created_at_can_be_set_by_raw_sql(w, admin):
    post(admin, "importata")
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE scheduling_timetablefeedback SET created_at = %s",
            [datetime.datetime(2020, 1, 1, tzinfo=datetime.UTC)],
        )
    assert TimetableFeedback.objects.get().created_at.year == 2020


# ---- no side effects ----------------------------------------------------------


def test_note_changes_nothing_else(w, admin):
    before_slots = list(w.tt.slots.order_by("pk").values())
    before = proposals.compute_proposals(w.school)
    version = Timetable.objects.get(pk=w.tt.pk).version
    post(admin, "neutra")
    assert Timetable.objects.get(pk=w.tt.pk).version == version
    assert list(w.tt.slots.order_by("pk").values()) == before_slots
    assert not SoftWeightAdjustment.objects.exists()
    assert proposals.compute_proposals(w.school) == before


def test_proposals_does_not_import_feedback():
    source = Path("scheduling/proposals.py").read_text()
    assert "feedback" not in source


# ---- audit ------------------------------------------------------------------------


def test_one_audit_entry_without_note_text(w, admin):
    post(admin, "segreto del preside")
    entry = AuditLog.objects.get(action=audit.FEEDBACK_ADDED)
    row = TimetableFeedback.objects.get()
    assert entry.details == {"feedback_id": row.pk, "lunghezza_nota": 19}
    assert "segreto" not in str(entry.details)
    assert (entry.week_start, entry.timetable_id) == (MON, w.tt.pk)
    assert "Nota sull&#x27;orario" in admin.get("/audit/").content.decode()


def test_failure_after_insert_leaves_nothing(w, admin):
    with mock.patch.object(feedback.audit, "record_audit", side_effect=RuntimeError):
        with pytest.raises(RuntimeError):
            feedback.add_feedback(w.tt.pk, user_of(admin), "x")
    assert not TimetableFeedback.objects.exists()
    assert not AuditLog.objects.filter(action=audit.FEEDBACK_ADDED).exists()


# ---- permission ----------------------------------------------------------------


def test_view_uses_the_predicate():
    assert permissions.can_leave_feedback in permissions.PREDICATES
