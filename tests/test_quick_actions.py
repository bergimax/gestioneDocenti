import datetime
from unittest import mock

import pytest
from django.test import Client

from scheduling import audit, tasks, views
from scheduling.context import build_week_context
from scheduling.derogations import QUICK_ACTIONS, REDUCE_CONTRACT_HOURS
from scheduling.models import JobKind, JobStatus, Slot, SolveJob, TimetableState
from scheduling.solver.context import (
    AUTHORIZE_EARLY_EXIT,
    AUTHORIZE_FRIDAY_LATE_START,
    AUTHORIZE_SHORT_PRATICA_BLOCK,
    AUTHORIZE_SINGLE_CLASSE_DAY,
    EXCLUDE_DAY,
    FORCE_3_HOURS_THEORY,
    OVERRIDE_AVAILABILITY,
    Derogation,
)

D = datetime.date
MON = D(2026, 10, 12)
BASE = f"/orario/{MON:%Y-%m-%d}/"
URL = BASE + "deroga/"
EIGHT = [
    FORCE_3_HOURS_THEORY,
    REDUCE_CONTRACT_HOURS,
    AUTHORIZE_EARLY_EXIT,
    OVERRIDE_AVAILABILITY,
    AUTHORIZE_SINGLE_CLASSE_DAY,
    AUTHORIZE_FRIDAY_LATE_START,
    AUTHORIZE_SHORT_PRATICA_BLOCK,
    EXCLUDE_DAY,
]


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    monkeypatch.setattr(views, "today", lambda: D(2026, 10, 14))


@pytest.fixture
def audit_mock():
    with mock.patch("scheduling.audit.record_audit") as m:
        yield m


def html(response):
    return response.content.decode()


def post(client, **data):
    return client.post(URL, {k: v for k, v in data.items() if v is not None})


def jobs():
    return SolveJob.objects.all()


def contractor(w):
    w.rossi.type = "CONTRATTO"
    w.rossi.save()


def run(job):
    tasks.run_job(job.pk)
    job.refresh_from_db()
    return job


class CaptureCtx:
    """Wraps solve_exclusive: records each solver input, runs the real solver."""

    def __init__(self):
        self.ctxs = []
        self._real = tasks.solve_exclusive

    def __call__(self, ctx, pinned=None, weights=None):
        self.ctxs.append(ctx)
        return self._real(ctx, pinned, weights)


@pytest.fixture
def capture(monkeypatch):
    c = CaptureCtx()
    monkeypatch.setattr(tasks, "solve_exclusive", c)
    return c


# ---- visibility and permissions ----


def test_admin_sees_the_form_with_the_eight_types(admin, w):
    page = html(admin.get(f"/orario/?settimana={MON:%Y-%m-%d}"))
    assert "Deroga" in page and 'name="azione"' in page
    for action in EIGHT:
        assert f'value="{action}"' in page
    assert page.count('data-fields="') == 9  # eight types and the placeholder


@pytest.mark.parametrize(
    "action, fields",
    [
        (FORCE_3_HOURS_THEORY, "classe docente giorno max_ore"),
        (REDUCE_CONTRACT_HOURS, "classe docente max_ore"),
        (AUTHORIZE_EARLY_EXIT, "classe docente giorno"),
        (OVERRIDE_AVAILABILITY, "classe docente giorno"),
        (AUTHORIZE_SINGLE_CLASSE_DAY, "classe docente giorno"),
        (AUTHORIZE_FRIDAY_LATE_START, "classe docente"),
        (AUTHORIZE_SHORT_PRATICA_BLOCK, "classe docente giorno"),
        (EXCLUDE_DAY, "docente giorno"),
    ],
)
def test_each_type_declares_only_its_parameters(admin, w, action, fields):
    page = html(admin.get(f"/orario/?settimana={MON:%Y-%m-%d}"))
    assert f'value="{action}" data-fields="{fields}"' in page


def test_segreteria_does_not_see_form_or_buttons(seg, w):
    w.tt.conflicts = [suggested_item("H4", OVERRIDE_AVAILABILITY, w)]
    w.tt.save()
    page = html(seg.get(f"/orario/?settimana={MON:%Y-%m-%d}"))
    assert (
        "<details" not in page
        and "Applica e rigenera" not in page
        and 'name="azione"' not in page
    )
    assert "tt-quick-btn" not in page
    assert "Ignora la disponibilità" not in page


def test_segreteria_post_is_403(seg, w, audit_mock):
    r = post(seg, azione=OVERRIDE_AVAILABILITY)
    assert r.status_code == 403
    assert jobs().count() == 0
    audit_mock.assert_not_called()


def test_segreteria_get_prefill_is_403(seg, w):
    assert seg.get(URL, {"azione": OVERRIDE_AVAILABILITY}).status_code == 403


def test_anonymous_is_redirected_to_login(db, w, audit_mock):
    c = Client()
    r = c.post(URL, {"azione": OVERRIDE_AVAILABILITY})
    assert r.status_code == 302 and "/login" in r["Location"]
    assert c.get(URL).status_code == 302
    assert jobs().count() == 0
    audit_mock.assert_not_called()


def test_approved_week_has_no_form_and_rejects_the_action(admin, w, audit_mock):
    w.tt.state = TimetableState.APPROVATO
    w.tt.save()
    page = html(admin.get(f"/orario/?settimana={MON:%Y-%m-%d}"))
    assert 'name="azione"' not in page
    r = post(admin, azione=OVERRIDE_AVAILABILITY)
    assert r.status_code == 409
    assert "approvata" in html(r)
    assert jobs().count() == 0
    audit_mock.assert_not_called()


def test_approved_week_prefill_is_refused(admin, w):
    w.tt.state = TimetableState.APPROVATO
    w.tt.save()
    r = admin.get(URL, {"azione": OVERRIDE_AVAILABILITY})
    assert r.status_code == 409 and 'name="azione"' not in html(r)


def test_week_without_timetable_is_refused(admin, w, audit_mock):
    w.tt.delete()
    page = html(admin.get(f"/orario/?settimana={MON:%Y-%m-%d}"))
    assert 'name="azione"' not in page
    r = post(admin, azione=OVERRIDE_AVAILABILITY)
    assert r.status_code == 409 and "genera prima" in html(r)
    assert jobs().count() == 0
    audit_mock.assert_not_called()


# ---- submitting ----


def test_submit_enqueues_one_job_with_the_derogation(admin, w, audit_mock):
    r = post(
        admin,
        azione=FORCE_3_HOURS_THEORY,
        classe=w.a.pk,
        docente=w.rossi.pk,
        giorno="LUNEDI",
        max_ore="4",
    )
    assert r.status_code == 200
    (job,) = jobs()
    assert job.kind == JobKind.GENERATE and job.status == JobStatus.PENDING
    assert job.params == {
        "quick_action": {
            "action_type": FORCE_3_HOURS_THEORY,
            "class_id": w.a.pk,
            "teacher_id": w.rossi.pk,
            "day": "LUNEDI",
            "max_hours": 4,
        }
    }
    assert "Generazione in corso" in html(r)
    w.tt.refresh_from_db()
    assert w.tt.version == 1  # only the finished regeneration bumps it


def test_scope_in_words_names_class_teacher_and_day(admin, w):
    r = post(admin, azione=OVERRIDE_AVAILABILITY, classe=w.a.pk, docente=w.rossi.pk)
    assert "applicata a: classe 1A, docente ROSSI MARIO." in html(r)


def test_scope_in_words_for_a_day(admin, w):
    r = post(admin, azione=OVERRIDE_AVAILABILITY, giorno="MARTEDI")
    assert "Deroga «Ignora la disponibilità» applicata a: martedì." in html(r)


def test_no_scope_applies_to_the_whole_week(admin, w):
    r = post(admin, azione=AUTHORIZE_EARLY_EXIT)
    assert "applicata a: tutta la settimana." in html(r)
    (job,) = jobs()
    q = job.params["quick_action"]
    assert (q["class_id"], q["teacher_id"], q["day"], q["max_hours"]) == (
        None,
        None,
        None,
        None,
    )


def test_parameters_that_do_not_apply_are_dropped(admin, w):
    post(
        admin,
        azione=AUTHORIZE_FRIDAY_LATE_START,
        classe=w.a.pk,
        giorno="LUNEDI",
        max_ore="5",
    )
    q = jobs().get().params["quick_action"]
    assert q["class_id"] == w.a.pk and q["day"] is None and q["max_hours"] is None


def test_reduce_drops_the_day_and_keeps_max_hours(admin, w):
    contractor(w)
    post(
        admin,
        azione=REDUCE_CONTRACT_HOURS,
        docente=w.rossi.pk,
        giorno="LUNEDI",
        max_ore="2",
    )
    q = jobs().get().params["quick_action"]
    assert q["day"] is None and q["max_hours"] == 2 and q["teacher_id"] == w.rossi.pk


def test_audit_is_called_once_with_the_parameters(admin, w, audit_mock):
    post(admin, azione=FORCE_3_HOURS_THEORY, classe=w.a.pk, giorno="LUNEDI")
    audit_mock.assert_called_once()
    action, user, week, params = audit_mock.call_args.args
    assert action == "QUICK_ACTION" == audit.QUICK_ACTION
    assert user.role == "ADMIN" and week == MON
    assert params == {
        "action_type": FORCE_3_HOURS_THEORY,
        "class_id": w.a.pk,
        "teacher_id": None,
        "day": "LUNEDI",
        "max_hours": None,
    }


def test_record_audit_writes_a_row(w, admin):
    from scheduling.models import AuditLog

    assert audit.record_audit("QUICK_ACTION", None, MON, {"a": 1}) is None
    entry = AuditLog.objects.get()
    assert (entry.action, entry.week_start, entry.details) == (
        "QUICK_ACTION",
        MON,
        {"a": 1},
    )
    assert entry.timetable_id == w.tt.pk and entry.user is None


# ---- validation ----


@pytest.mark.parametrize("bad", ["0", "-1", "abc", "31", "1.5", "99999999999"])
def test_bad_max_hours_is_a_form_error_and_enqueues_nothing(admin, w, audit_mock, bad):
    r = post(admin, azione=FORCE_3_HOURS_THEORY, max_ore=bad)
    assert r.status_code == 400
    assert "numero intero da 1 a 30" in html(r)
    assert jobs().count() == 0
    audit_mock.assert_not_called()


@pytest.mark.parametrize("good, stored", [("1", 1), ("30", 30), ("", None), ("12", 12)])
def test_max_hours_boundaries_and_optional_default(admin, w, good, stored):
    r = post(admin, azione=FORCE_3_HOURS_THEORY, max_ore=good)
    assert r.status_code == 200
    assert jobs().get().params["quick_action"]["max_hours"] == stored


def test_max_hours_is_checked_even_for_a_type_that_ignores_it(admin, w):
    assert post(admin, azione=AUTHORIZE_EARLY_EXIT, max_ore="0").status_code == 400


@pytest.mark.parametrize(
    "extra, message",
    [
        ({"classe": "9999"}, "La classe indicata non esiste."),
        ({"classe": "abc"}, "La classe indicata non esiste."),
        ({"docente": "9999"}, "Il docente indicato non esiste."),
        ({"giorno": "SABATO"}, "solo i giorni feriali"),
        ({"giorno": "DOMENICA"}, "solo i giorni feriali"),
    ],
)
def test_unknown_class_teacher_or_day_is_rejected(admin, w, audit_mock, extra, message):
    r = post(admin, azione=OVERRIDE_AVAILABILITY, **extra)
    assert r.status_code == 400 and message in html(r)
    assert jobs().count() == 0
    audit_mock.assert_not_called()


def test_a_class_of_another_school_does_not_exist(admin, w):
    from school.models import Class, School

    other = School.objects.create(
        name="Altra",
        formative_year="2026-2027",
        year_start=D(2026, 9, 1),
        year_end=D(2027, 6, 30),
    )
    foreign = Class.objects.create(school=other, name="9Z")
    r = post(admin, azione=OVERRIDE_AVAILABILITY, classe=foreign.pk)
    assert r.status_code == 400 and jobs().count() == 0


@pytest.mark.parametrize("action", [None, "", "assign_teacher", "manual_edit", "nope"])
def test_only_the_eight_types_are_accepted(admin, w, audit_mock, action):
    r = post(admin, azione=action)
    assert r.status_code == 400 and jobs().count() == 0
    audit_mock.assert_not_called()


def test_error_keeps_the_submitted_values(admin, w):
    r = post(admin, azione=FORCE_3_HOURS_THEORY, classe=w.a.pk, max_ore="0")
    page = html(r)
    assert f'<option value="{w.a.pk}" selected>' in page
    assert (
        f'value="{FORCE_3_HOURS_THEORY}" data-fields="classe docente giorno '
        'max_ore" selected' in page
    )


def test_reduce_for_an_assunto_teacher_is_rejected(admin, w, audit_mock):
    r = post(admin, azione=REDUCE_CONTRACT_HOURS, docente=w.rossi.pk)
    assert r.status_code == 400
    assert "solo ai docenti a contratto" in html(r)
    assert jobs().count() == 0
    audit_mock.assert_not_called()


def test_reduce_for_a_contract_teacher_is_accepted(admin, w):
    contractor(w)
    assert (
        post(admin, azione=REDUCE_CONTRACT_HOURS, docente=w.rossi.pk).status_code == 200
    )
    assert jobs().count() == 1


def test_reduce_without_teacher_is_accepted(admin, w):
    assert post(admin, azione=REDUCE_CONTRACT_HOURS).status_code == 200


def test_assunto_check_applies_to_reduce_only(admin, w):
    assert (
        post(admin, azione=OVERRIDE_AVAILABILITY, docente=w.rossi.pk).status_code == 200
    )


# ---- one regeneration at a time ----


def test_second_submission_is_refused_while_a_job_is_active(admin, w, audit_mock):
    assert post(admin, azione=OVERRIDE_AVAILABILITY).status_code == 200
    r = post(admin, azione=AUTHORIZE_EARLY_EXIT, classe=w.a.pk)
    assert r.status_code == 409
    assert "già in corso" in html(r)
    assert jobs().count() == 1
    assert jobs().get().params["quick_action"]["action_type"] == OVERRIDE_AVAILABILITY
    audit_mock.assert_called_once()


def test_running_job_also_blocks(admin, w):
    post(admin, azione=OVERRIDE_AVAILABILITY)
    SolveJob.objects.update(status=JobStatus.RUNNING)
    assert post(admin, azione=OVERRIDE_AVAILABILITY).status_code == 409
    assert jobs().count() == 1


def test_finished_job_does_not_block(admin, w):
    post(admin, azione=OVERRIDE_AVAILABILITY)
    SolveJob.objects.update(status=JobStatus.DONE)
    assert post(admin, azione=AUTHORIZE_EARLY_EXIT).status_code == 200
    assert jobs().count() == 2


def test_refused_second_submission_keeps_the_form_values(admin, w):
    post(admin, azione=OVERRIDE_AVAILABILITY)
    r = post(admin, azione=AUTHORIZE_EARLY_EXIT, classe=w.b.pk)
    assert f'<option value="{w.b.pk}" selected>' in html(r)


def test_enqueue_failure_leaves_no_job_and_no_audit(admin, w, audit_mock):
    with mock.patch("scheduling.jobs.run_solve_job") as task:
        task.enqueue.side_effect = OSError("x")
        with pytest.raises(OSError):
            post(admin, azione=OVERRIDE_AVAILABILITY)
    assert jobs().count() == 0
    audit_mock.assert_not_called()


# ---- the regeneration ----


def expected(action, w):
    return {
        FORCE_3_HOURS_THEORY: Derogation(
            FORCE_3_HOURS_THEORY, w.a.pk, w.rossi.pk, 0, 4
        ),
        REDUCE_CONTRACT_HOURS: Derogation(
            REDUCE_CONTRACT_HOURS, w.a.pk, w.rossi.pk, None, 2
        ),
        AUTHORIZE_EARLY_EXIT: Derogation(AUTHORIZE_EARLY_EXIT, w.a.pk, w.rossi.pk, 0),
        OVERRIDE_AVAILABILITY: Derogation(OVERRIDE_AVAILABILITY, w.a.pk, w.rossi.pk, 0),
        AUTHORIZE_SINGLE_CLASSE_DAY: Derogation(
            AUTHORIZE_SINGLE_CLASSE_DAY, w.a.pk, w.rossi.pk, 0
        ),
        AUTHORIZE_FRIDAY_LATE_START: Derogation(
            AUTHORIZE_FRIDAY_LATE_START, w.a.pk, w.rossi.pk
        ),
        AUTHORIZE_SHORT_PRATICA_BLOCK: Derogation(
            AUTHORIZE_SHORT_PRATICA_BLOCK, w.a.pk, w.rossi.pk, 0
        ),
        EXCLUDE_DAY: Derogation(EXCLUDE_DAY, None, w.rossi.pk, 0),
    }[action]


@pytest.mark.parametrize("action", EIGHT)
def test_solver_receives_the_derogation_for_that_regeneration_only(
    admin, w, capture, action
):
    contractor(w)
    post(
        admin,
        azione=action,
        classe=w.a.pk,
        docente=w.rossi.pk,
        giorno="LUNEDI",
        max_ore="4" if action == FORCE_3_HOURS_THEORY else "2",
    )
    job = run(jobs().get())
    assert job.status == JobStatus.DONE
    assert capture.ctxs[-1].derogations == [expected(action, w)]
    w.tt.refresh_from_db()
    assert w.tt.version == 2  # bumped once, by the one regeneration

    # Regenerating again without a form submission: no derogation anywhere.
    again = SolveJob.objects.create(school=w.school, week_start=MON)
    assert again.params == {}
    run(again)
    assert capture.ctxs[-1].derogations == []
    assert capture.ctxs[-1].targets == build_week_context(MON, w.school).targets
    w.tt.refresh_from_db()
    assert w.tt.version == 3


def test_plain_generation_after_a_quick_action_has_no_derogation(admin, w, capture):
    post(admin, azione=AUTHORIZE_EARLY_EXIT)
    run(jobs().get())
    admin.post(BASE + "genera/")
    plain = jobs().order_by("-pk").first()
    assert plain.params == {}
    run(plain)
    assert capture.ctxs[-1].derogations == []


def test_no_derogation_is_stored_on_the_school_teacher_or_week(admin, w):
    from django.apps import apps

    post(admin, azione=OVERRIDE_AVAILABILITY, docente=w.rossi.pk)
    run(jobs().get())
    for model in apps.get_app_config("scheduling").get_models():
        names = {f.name for f in model._meta.get_fields()}
        assert not {n for n in names if "derog" in n or "quick" in n}, model
    w.tt.refresh_from_db()
    w.rossi.refresh_from_db()
    w.school.refresh_from_db()
    for obj in (w.tt, w.rossi, w.school):
        assert "OVERRIDE_AVAILABILITY".lower() not in repr(vars(obj)).lower()


def test_override_availability_changes_the_result(admin, w):
    """A contractor without a grid is unavailable all week until the derogation."""
    contractor(w)
    w.rossi.first_time = False
    w.rossi.save()
    again = SolveJob.objects.create(school=w.school, week_start=MON)
    run(again)
    without = Slot.objects.filter(timetable=w.tt, teacher=w.rossi).count()
    assert without == 0
    post(admin, azione=OVERRIDE_AVAILABILITY, docente=w.rossi.pk)
    run(jobs().order_by("-pk").first())
    with_it = Slot.objects.filter(timetable=w.tt, teacher=w.rossi).count()
    assert with_it > without
    # and the next plain regeneration is back to the first result
    run(SolveJob.objects.create(school=w.school, week_start=MON))
    assert Slot.objects.filter(timetable=w.tt, teacher=w.rossi).count() == without


def test_regeneration_bumps_version_once_and_the_grid_refreshes(admin, w):
    post(admin, azione=AUTHORIZE_EARLY_EXIT)
    run(jobs().get())
    w.tt.refresh_from_db()
    assert w.tt.version == 2
    assert admin.get(BASE + "griglia/?versione=1").status_code == 200
    assert admin.get(BASE + "griglia/?versione=2").status_code == 204


def test_finished_job_poll_refreshes_the_deroga_panel(admin, w):
    post(admin, azione=AUTHORIZE_EARLY_EXIT)
    run(jobs().get())
    r = admin.get(BASE + "stato/")
    assert 'id="tt-deroga-panel" hx-swap-oob="true"' in html(r)
    assert 'id="timetable-body" hx-swap-oob="true"' in html(r)


# ---- reduce_contract_hours in the context builder ----


def targets_by_teacher(w, derogations=()):
    ctx = build_week_context(MON, w.school, derogations=derogations)
    return {
        (a.teacher_id, a.class_id): ctx.targets.get(a.id) for a in ctx.assignments
    }, ctx


def test_reduce_halves_the_target_of_a_contractor(w):
    contractor(w)
    base, _ = targets_by_teacher(w)
    reduced, ctx = targets_by_teacher(
        w, [Derogation(REDUCE_CONTRACT_HOURS, teacher_id=w.rossi.pk)]
    )
    key = (w.rossi.pk, w.a.pk)
    assert base[key] == 1 and reduced[key] == max(1, base[key] // 2)
    assert ctx.derogations == [Derogation(REDUCE_CONTRACT_HOURS, teacher_id=w.rossi.pk)]


def big_hours(w):
    from school.models import Assignment

    Assignment.objects.update(ore_totali=370)


def test_reduce_target_rule_halves_without_max_hours(w):
    contractor(w)
    big_hours(w)
    base, _ = targets_by_teacher(w)
    reduced, _ = targets_by_teacher(w, [Derogation(REDUCE_CONTRACT_HOURS)])
    key = (w.rossi.pk, w.a.pk)
    assert base[key] == 10 and reduced[key] == 5


def test_reduce_with_max_hours_caps_the_target(w):
    contractor(w)
    big_hours(w)
    capped, _ = targets_by_teacher(w, [Derogation(REDUCE_CONTRACT_HOURS, max_hours=3)])
    assert capped[w.rossi.pk, w.a.pk] == 3
    high, _ = targets_by_teacher(w, [Derogation(REDUCE_CONTRACT_HOURS, max_hours=20)])
    assert high[w.rossi.pk, w.a.pk] == 10  # min(target, max_hours)


def test_reduce_never_touches_an_assunto_teacher(w):
    big_hours(w)
    base, _ = targets_by_teacher(w)
    reduced, _ = targets_by_teacher(w, [Derogation(REDUCE_CONTRACT_HOURS)])
    assert reduced == base


def test_reduce_respects_its_class_and_teacher_scope(w):
    contractor(w)
    w.bianchi.type = "CONTRATTO"
    w.bianchi.save()
    big_hours(w)
    base, _ = targets_by_teacher(w)
    by_teacher, _ = targets_by_teacher(
        w, [Derogation(REDUCE_CONTRACT_HOURS, teacher_id=w.rossi.pk)]
    )
    assert by_teacher[w.rossi.pk, w.a.pk] == 5
    assert by_teacher[w.bianchi.pk, w.a.pk] == base[w.bianchi.pk, w.a.pk]
    by_class, _ = targets_by_teacher(
        w, [Derogation(REDUCE_CONTRACT_HOURS, class_id=w.b.pk)]
    )
    assert by_class[w.rossi.pk, w.b.pk] == 5
    assert by_class[w.rossi.pk, w.a.pk] == base[w.rossi.pk, w.a.pk]


def test_context_without_derogations_is_unchanged(w):
    assert build_week_context(MON, w.school).derogations == []


# ---- suggested actions ----


def suggested_item(kind, action, w, **kw):
    item = {
        "kind": kind,
        "chiave": f"{kind}|{w.a.pk}|{w.rossi.pk}|LUNEDI",
        "description": f"conflitto {kind}",
        "suggested_action": {"action_type": action, "label": f"Etichetta {action}"},
        "classe_id": w.a.pk,
        "docente_id": w.rossi.pk,
        "giorno": "LUNEDI",
    }
    item.update(kw)
    return item


def test_each_conflict_shows_its_suggested_action_as_a_button(admin, w):
    w.tt.conflicts = [
        suggested_item("H4", OVERRIDE_AVAILABILITY, w),
        suggested_item("H9", AUTHORIZE_FRIDAY_LATE_START, w, giorno="VENERDI"),
    ]
    w.tt.save()
    page = html(admin.get(BASE + "griglia/"))
    assert page.count("tt-quick-btn") == 2
    assert "Etichetta override_availability" in page
    assert (
        f"deroga/?azione=override_availability&amp;classe={w.a.pk}"
        f"&amp;docente={w.rossi.pk}&amp;giorno=LUNEDI"
    ) in page
    assert "giorno=VENERDI" in page


def test_assign_teacher_and_manual_edit_are_not_quick_actions(admin, w):
    w.tt.conflicts = [
        suggested_item("classe_unfilled", "assign_teacher", w),
        suggested_item("X", "manual_edit", w),
    ]
    w.tt.save()
    page = html(admin.get(BASE + "griglia/"))
    assert "conflitto classe_unfilled" in page and "conflitto X" in page
    assert "tt-quick-btn" not in page


def test_conflict_without_scope_prefills_an_empty_scope(admin, w):
    w.tt.conflicts = [
        suggested_item(
            "ore_target_deviation",
            REDUCE_CONTRACT_HOURS,
            w,
            classe_id=None,
            docente_id=None,
            giorno=None,
        )
    ]
    w.tt.save()
    page = html(admin.get(BASE + "griglia/"))
    assert "azione=reduce_contract_hours&amp;classe=&amp;docente=&amp;giorno=" in page


def test_buttons_are_hidden_on_an_approved_week(admin, w):
    w.tt.conflicts = [suggested_item("H4", OVERRIDE_AVAILABILITY, w)]
    w.tt.state = TimetableState.APPROVATO
    w.tt.save()
    assert "tt-quick-btn" not in html(admin.get(BASE + "griglia/"))


def test_prefill_opens_the_form_with_the_conflict_values(admin, w):
    r = admin.get(
        URL,
        {
            "azione": OVERRIDE_AVAILABILITY,
            "classe": w.a.pk,
            "docente": w.rossi.pk,
            "giorno": "LUNEDI",
        },
    )
    page = html(r)
    assert r.status_code == 200
    assert '<details class="tt-deroga" open>' in page
    assert (
        f'value="{OVERRIDE_AVAILABILITY}" data-fields="classe docente giorno" selected'
        in page
    )
    assert f'<option value="{w.a.pk}" selected>' in page
    assert f'<option value="{w.rossi.pk}" selected>' in page
    assert '<option value="LUNEDI" selected>' in page


def test_prefill_ignores_garbage_values(admin, w):
    r = admin.get(URL, {"azione": "zzz", "classe": "x", "docente": "-", "giorno": "?"})
    assert r.status_code == 200 and "selected" in html(r)


def test_questions_of_the_form_only_allow_get_and_post(admin, w):
    assert admin.put(URL).status_code == 405


def test_all_quick_action_types_are_catalogued():
    assert set(QUICK_ACTIONS) == set(EIGHT)


# ---- exclude_day ----


def test_exclude_day_stores_only_teacher_and_day(admin, w, audit_mock):
    r = post(
        admin,
        azione=EXCLUDE_DAY,
        docente=w.rossi.pk,
        giorno="GIOVEDI",
        classe=w.a.pk,
        max_ore="3",
    )
    assert r.status_code == 200
    expected_params = {
        "action_type": "exclude_day",
        "class_id": None,
        "teacher_id": w.rossi.pk,
        "day": "GIOVEDI",
        "max_hours": None,
    }
    assert jobs().get().params == {"quick_action": expected_params}
    audit_mock.assert_called_once()
    assert audit_mock.call_args.args[3] == expected_params


def test_exclude_day_reaches_the_solver_and_is_not_persisted(admin, w, capture):
    post(admin, azione=EXCLUDE_DAY, docente=w.rossi.pk, giorno="MARTEDI")
    run(jobs().get())
    assert capture.ctxs[-1].excluded_days == {w.rossi.pk: frozenset({1})}
    again = SolveJob.objects.create(school=w.school, week_start=MON)
    run(again)
    assert capture.ctxs[-1].excluded_days == {}


@pytest.mark.parametrize("missing", ["docente", "giorno"])
def test_exclude_day_needs_teacher_and_day(admin, w, audit_mock, missing):
    data = {"docente": w.rossi.pk, "giorno": "LUNEDI"}
    del data[missing]
    r = post(admin, azione=EXCLUDE_DAY, **data)
    assert r.status_code == 400 and jobs().count() == 0
    audit_mock.assert_not_called()


def test_exclude_day_unknown_teacher_is_rejected(admin, w, audit_mock):
    r = post(admin, azione=EXCLUDE_DAY, docente=999999, giorno="LUNEDI")
    assert r.status_code == 400 and jobs().count() == 0
    audit_mock.assert_not_called()
