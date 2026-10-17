"""Audit entries for the administrative events (#73): weights, repair dismissal,
availability links and the setup save. One row per successful mutation, written
in its transaction, ids and constant codes only."""

import datetime
import json
from unittest import mock

import pytest
from django.db import transaction
from django.test import Client

from accounts.models import User
from availability import links
from availability.models import AvailabilityLink
from scheduling import audit
from scheduling.models import AuditLog, SoftWeightAdjustment
from scheduling.proposals import apply_proposals, reset_weights
from school.models import School, Teacher
from school.setup_service import replace_counts, save_setup
from tests import test_repair as rep
from tests import test_setup_replace as rpl
from tests import test_setup_wizard as wiz
from tests.test_weight_proposals import S1, S2, pick

# fixtures of the suites this one builds on, reused as they are
rw = rep.rw
rep_admin = rep.admin
rep_seg = rep.seg
old = rpl.old
admin = rpl.admin
wiz_admin = wiz.admin

D = datetime.date
NEW = (
    audit.SOFT_WEIGHTS_APPLIED,
    audit.SOFT_WEIGHTS_RESET,
    audit.REPAIR_DISMISSED,
    audit.AVAILABILITY_LINK_CREATED,
    audit.AVAILABILITY_LINK_REVOKED,
    audit.SETUP_SAVED,
)
W1 = D(2026, 9, 7)
ALL = {"all": "1"}


def entries(action=None):
    qs = AuditLog.objects.order_by("pk")
    return list(qs.filter(action=action) if action else qs)


def write_then_fail(action, user, week, params):
    AuditLog.objects.create(
        school=School.objects.first(), action=action, details=params
    )
    raise RuntimeError("boom")


def assert_plain(details):
    """Only ints, bools, short codes, or lists/dicts of those."""
    if isinstance(details, dict):
        for k, v in details.items():
            assert isinstance(k, str)
            assert_plain(v)
    elif isinstance(details, list):
        for v in details:
            assert_plain(v)
    else:
        assert isinstance(details, (int, bool, str)) and details is not None
        if isinstance(details, str):
            assert "http" not in details and "/" not in details


def login(email, role):
    c = Client()
    c.user = User.objects.create_user(email, "pw-long-enough", role)
    c.force_login(c.user)
    return c


@pytest.fixture
def school(db):
    return School.objects.create(
        name="S",
        formative_year="2026-2027",
        year_start=D(2026, 9, 1),
        year_end=D(2027, 6, 30),
    )


@pytest.fixture
def adm(school):
    return login("a@example.com", "ADMIN")


@pytest.fixture
def sg(school):
    return login("s@example.com", "SEGRETERIA")


@pytest.fixture
def teacher(school):
    return Teacher.objects.create(
        school=school, name="ROSSI", type="CONTRATTO", first_time=False
    )


def test_labels_cover_the_new_actions():
    for action in NEW:
        assert action in audit.ACTIONS and audit.ACTION_LABELS[action]
    assert len(audit.ACTIONS) == 17


# ---- weights ----------------------------------------------------------------


def two_proposals(school):
    for _ in range(2):
        pick(school, {S1: 0, S2: 0}, {S1: 3, S2: 3})


def test_apply_writes_one_entry_with_the_rows(adm, school):
    two_proposals(school)
    assert adm.post("/pesi/applica/", ALL).status_code == 302
    rows = list(SoftWeightAdjustment.objects.order_by("pk"))
    assert len(rows) == 2
    (entry,) = entries(audit.SOFT_WEIGHTS_APPLIED)
    assert entry.details == {
        "kinds": [r.kind for r in rows],
        "adjustments": [r.pk for r in rows],
    }
    assert entry.user_id == adm.user.pk and entry.week_start is None
    assert_plain(entry.details)


def test_apply_with_nothing_to_apply_writes_nothing(adm, school):
    adm.post("/pesi/applica/", ALL)
    assert entries() == []


def test_reset_writes_one_entry_and_skips_defaults(adm, school):
    two_proposals(school)
    adm.post("/pesi/applica/", ALL)
    adm.post("/pesi/ripristina/", {"kind": [S1]})
    (entry,) = entries(audit.SOFT_WEIGHTS_RESET)
    row = SoftWeightAdjustment.objects.order_by("pk").last()
    assert entry.details == {"kinds": [S1], "adjustments": [row.pk]}
    assert entry.user_id == adm.user.pk
    adm.post("/pesi/ripristina/", {"kind": [S1]})  # already default
    assert len(entries(audit.SOFT_WEIGHTS_RESET)) == 1


def test_segreteria_cannot_apply_or_reset(sg, school):
    two_proposals(school)
    assert sg.post("/pesi/applica/", ALL).status_code == 403
    assert sg.post("/pesi/ripristina/", ALL).status_code == 403
    assert entries() == [] and SoftWeightAdjustment.objects.count() == 0


def test_weights_rollback(adm, school):
    two_proposals(school)
    with mock.patch("scheduling.audit.record_audit", side_effect=write_then_fail):
        with pytest.raises(RuntimeError):
            apply_proposals(school, adm.user)
    assert SoftWeightAdjustment.objects.count() == 0 and entries() == []
    apply_proposals(school, adm.user)
    AuditLog.objects.all()  # one entry from the real apply
    before = (SoftWeightAdjustment.objects.count(), AuditLog.objects.count())
    with mock.patch("scheduling.audit.record_audit", side_effect=write_then_fail):
        with pytest.raises(RuntimeError):
            reset_weights(school, adm.user)
    assert (SoftWeightAdjustment.objects.count(), AuditLog.objects.count()) == before


# ---- repair dismissal -------------------------------------------------------


def test_dismiss_writes_one_entry(rep_admin, rw):
    job = rep.start(rep_admin, rep.absent(rw))
    before = AuditLog.objects.count()
    assert rep_admin.post(rep.dismiss_url(job)).status_code == 200
    assert AuditLog.objects.count() == before + 1
    (entry,) = entries(audit.REPAIR_DISMISSED)
    assert entry.week_start == job.week_start and entry.details == {"job": job.pk}
    assert entry.user_id == User.objects.get(email="a@example.com").pk
    job.refresh_from_db()
    assert job.result["dismissed"] is True


def test_refused_dismissals_write_nothing(rep_admin, rep_seg, rw):
    applied = rep.start(rep_admin, rep.absent(rw, days=("MARTEDI",)))
    assert rep_admin.post(rep.confirm_url(applied)).status_code == 200
    job = rep.start(rep_admin, rep.absent(rw))
    assert rep_seg.post(rep.dismiss_url(job)).status_code == 403
    assert rep_admin.post(f"{rep.URL}99999/annulla/").status_code == 404
    assert rep_admin.post(rep.dismiss_url(applied)).status_code == 409
    rep_admin.post(rep.dismiss_url(job))
    rep_admin.post(rep.dismiss_url(job))  # already dismissed: not a second entry
    assert len(entries(audit.REPAIR_DISMISSED)) == 1
    pending = rep_admin.post(rep.URL, rep.absent(rw, days=("GIOVEDI",)))
    assert pending.status_code == 200
    not_done = rep.job_of()
    assert not_done.status != "DONE"
    assert rep_admin.post(rep.dismiss_url(not_done)).status_code == 409
    assert len(entries(audit.REPAIR_DISMISSED)) == 1


def test_dismiss_rollback(rep_admin, rw):
    job = rep.start(rep_admin, rep.absent(rw))
    user = User.objects.get(email="a@example.com")
    before = AuditLog.objects.count()
    with mock.patch("scheduling.audit.record_audit", side_effect=write_then_fail):
        with pytest.raises(RuntimeError):
            rep.repair.dismiss_repair(job.pk, user)
    job.refresh_from_db()
    assert not job.result["dismissed"] and AuditLog.objects.count() == before


# ---- availability links -----------------------------------------------------


def test_create_link_entry_and_replacement(teacher, adm):
    first, token = links.create_link(teacher, W1, adm.user)
    (entry,) = entries()
    assert entry.action == audit.AVAILABILITY_LINK_CREATED
    assert entry.week_start == W1
    assert entry.details == {"link": first.pk, "teacher": teacher.pk, "revoked": 0}
    second, token2 = links.create_link(teacher, W1, adm.user)
    created = entries(audit.AVAILABILITY_LINK_CREATED)
    assert len(created) == 2 and not entries(audit.AVAILABILITY_LINK_REVOKED)
    assert created[1].details["revoked"] == 1
    for e in entries():
        blob = json.dumps(e.details)
        for secret in (token, token2, first.token_hash, second.token_hash):
            assert secret not in blob
        assert "/disponibilita" not in blob and "http" not in blob
        assert_plain(e.details)


def test_link_error_writes_nothing(teacher, adm, monkeypatch):
    with pytest.raises(links.LinkError):
        links.create_link(teacher, D(2026, 9, 8), adm.user)  # not a Monday
    with pytest.raises(links.LinkError):
        links.create_link(teacher, D(2030, 9, 2), adm.user)  # outside the year
    monkeypatch.setattr(links.services, "week_is_approved", lambda *a, **k: True)
    with pytest.raises(links.LinkError):
        links.create_link(teacher, W1, adm.user)
    assert entries() == [] and AvailabilityLink.objects.count() == 0


def test_revoke_entry(teacher, adm):
    link, _ = links.create_link(teacher, W1, adm.user)
    url = f"/disponibilita/link/{link.pk}/revoca/"
    adm.post(url)
    (entry,) = entries(audit.AVAILABILITY_LINK_REVOKED)
    assert entry.details == {"link": link.pk, "teacher": teacher.pk}
    assert entry.user_id == adm.user.pk and entry.week_start == W1
    adm.post(url)  # already revoked
    assert len(entries(audit.AVAILABILITY_LINK_REVOKED)) == 1


def test_revoke_noops_write_nothing(teacher, adm, school):
    link, _ = links.create_link(teacher, W1, adm.user)
    AvailabilityLink.objects.filter(pk=link.pk).update(
        expires_at=links.now() - datetime.timedelta(hours=1)
    )
    other = School.objects.create(
        name="Altra",
        formative_year="2026-2027",
        year_start=D(2026, 9, 1),
        year_end=D(2027, 6, 30),
    )
    foreign_teacher = Teacher.objects.create(
        school=other, name="X", type="CONTRATTO", first_time=False
    )
    foreign = AvailabilityLink.objects.create(
        teacher=foreign_teacher,
        week_start=W1,
        token_hash="f" * 64,
        expires_at=links.now() + datetime.timedelta(hours=5),
    )
    for pk in (link.pk, foreign.pk, 99999):
        adm.post(f"/disponibilita/link/{pk}/revoca/")
    assert not entries(audit.AVAILABILITY_LINK_REVOKED)
    foreign.refresh_from_db()
    assert foreign.revoked_at is None


# ---- setup ------------------------------------------------------------------


def last_setup_entry():
    (entry,) = entries(audit.SETUP_SAVED)
    return entry


def test_first_setup_entry(wiz_admin):
    wiz.upload_all(wiz_admin)
    r = wiz_admin.post(
        wiz.SAVE, {"school_name": "Scuola Unica", "school_year": "2026-2027"}
    )
    assert r.status_code == 302
    entry = last_setup_entry()
    assert entry.details == {"replaced": False, "created": wiz.COUNTS, "deleted": {}}
    assert entry.week_start is None and entry.user.role == "ADMIN"
    assert "Scuola Unica" not in json.dumps(entry.details)
    assert_plain(entry.details)


def test_replace_entry_survives(admin, old):
    wiz.upload_all(admin)
    before = list(AuditLog.objects.order_by("pk").values())
    expected = replace_counts()
    assert rpl.confirm(admin).status_code == 302
    entry = last_setup_entry()
    assert entry.details["replaced"] is True
    assert entry.details["created"] == wiz.COUNTS
    assert entry.details["deleted"] == expected
    assert "Vecchia" not in json.dumps(entry.details)
    assert "Nuova" not in json.dumps(entry.details)
    after = list(AuditLog.objects.exclude(pk=entry.pk).order_by("pk").values())
    # the old rows lose only their (deleted) timetable link
    for b, a in zip(before, after, strict=True):
        b.pop("timetable_id"), a.pop("timetable_id")
        assert a == b


def test_setup_failures_write_nothing(admin, old):
    wiz.upload_all(admin)
    n = AuditLog.objects.count()
    assert rpl.confirm(admin, "sbagliata").status_code == 422
    with mock.patch(
        "school.setup_service._create_pairings", side_effect=RuntimeError("x")
    ):
        assert rpl.confirm(admin).status_code == 500
    with mock.patch(
        "school.setup_service._create_classes", side_effect=RuntimeError("x")
    ):
        assert rpl.confirm(admin).status_code == 500
    assert AuditLog.objects.count() == n and not entries(audit.SETUP_SAVED)
    assert School.objects.get().name == "Vecchia"


def test_save_setup_without_user_is_allowed(wiz_admin):
    wiz.upload_all(wiz_admin)
    with transaction.atomic():
        save_setup("Scuola", "2026-2027", None, None)
    assert last_setup_entry().user is None


# ---- the audit screen -------------------------------------------------------


def test_screen_filters_each_new_action(adm, school):
    for action in NEW:
        audit.record_audit(action, adm.user, None, {"n": 1})
    audit.record_audit(audit.GENERATE, adm.user, None, {})
    page = adm.get("/audit/").content.decode()
    for action in NEW:
        assert f'value="{action}"' in page
    for action in NEW:
        body = adm.get("/audit/", {"azione": action}).content.decode()
        assert audit.ACTION_LABELS[action] in body
        for other in NEW:
            if other != action:
                assert f"<td>{audit.ACTION_LABELS[other]}</td>" not in body
        assert audit.ACTION_LABELS[audit.GENERATE] not in body.split("<tbody")[-1]


def test_segreteria_is_refused_on_the_screen(sg):
    assert sg.get("/audit/", {"azione": audit.SETUP_SAVED}).status_code == 403
