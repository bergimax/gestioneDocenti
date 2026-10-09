import datetime

import pytest
from django.contrib import admin
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError

from scheduling.models import Slot, Timetable
from school.models import Class, School, Subject, Teacher

D = datetime.date
MON = D(2026, 9, 7)


def make_school(name="A"):
    return School.objects.create(
        name=name,
        formative_year="2026-2027",
        year_start=D(2026, 9, 1),
        year_end=D(2027, 6, 30),
    )


class Ctx:
    def __init__(self, school):
        self.school = school
        self.tt = Timetable.objects.create(school=school, week_start=MON)
        self.cls = Class.objects.create(school=school, name="1A")
        self.cls2 = Class.objects.create(school=school, name="1B")
        self.teacher = Teacher.objects.create(
            school=school, name="ROSSI", type="ASSUNTO"
        )
        self.subject = Subject.objects.create(
            school=school, name="MAT", kind="TEORIA", cognitive_weight="ALTO"
        )

    def slot(self, **kw):
        kw.setdefault("timetable", self.tt)
        kw.setdefault("school_class", self.cls)
        kw.setdefault("teacher", self.teacher)
        kw.setdefault("subject", self.subject)
        kw.setdefault("day", "LUNEDI")
        kw.setdefault("start_hour", 8)
        kw.setdefault("end_hour", 9)
        return Slot(**kw)


@pytest.fixture
def ctx(db):
    return Ctx(make_school())


@pytest.fixture
def other(db):
    return Ctx(make_school("B"))


def test_registered_in_admin():
    assert Timetable in admin.site._registry
    assert Slot in admin.site._registry
    ro = admin.site._registry[Timetable].readonly_fields
    assert {"state", "approved_at", "version"} <= set(ro)


def test_timetable_defaults(ctx):
    t = ctx.tt
    assert t.state == "BOZZA"
    assert t.version == 0
    assert t.quality_score is None and t.quality_level is None
    assert t.approved_at is None
    assert t.soft_conflicts == 0
    t.full_clean()


def test_unique_school_week(ctx, other):
    with pytest.raises(IntegrityError), transaction.atomic():
        Timetable.objects.create(school=ctx.school, week_start=MON)
    # same week for another school exists already (other fixture)
    assert Timetable.objects.filter(week_start=MON).count() == 2


def test_non_monday_rejected(ctx):
    t = Timetable(school=ctx.school, week_start=D(2026, 9, 8))
    with pytest.raises(ValidationError) as e:
        t.full_clean(validate_unique=False)
    assert "week_start" in e.value.message_dict


@pytest.mark.parametrize("score", [-1, 100.1])
def test_bad_score_rejected(ctx, score):
    ctx.tt.quality_score = score
    with pytest.raises(ValidationError) as e:
        ctx.tt.full_clean()
    assert "quality_score" in e.value.message_dict


@pytest.mark.parametrize("score", [0, 100])
def test_score_bounds_accepted(ctx, score):
    ctx.tt.quality_score = score
    ctx.tt.full_clean()


def test_bad_level_rejected(ctx):
    ctx.tt.quality_level = "D"
    with pytest.raises(ValidationError) as e:
        ctx.tt.full_clean()
    assert "quality_level" in e.value.message_dict


def test_valid_slot(ctx):
    s = ctx.slot()
    s.full_clean()
    s.save()


def test_valid_paired_slot(ctx):
    s = ctx.slot(paired=True, paired_class=ctx.cls2)
    s.full_clean()


def bad(ctx, field, **kw):
    with pytest.raises(ValidationError) as e:
        ctx.slot(**kw).full_clean()
    assert field in e.value.message_dict


def test_unknown_day(ctx):
    bad(ctx, "day", day="SABATO")


def test_start_hour_7(ctx):
    bad(ctx, "start_hour", start_hour=7, end_hour=8)


def test_start_hour_14(ctx):
    bad(ctx, "start_hour", start_hour=14, end_hour=15)


def test_end_hour_8(ctx):
    bad(ctx, "end_hour", start_hour=7, end_hour=8)


def test_end_hour_15(ctx):
    bad(ctx, "end_hour", start_hour=14, end_hour=15)


def test_end_not_start_plus_one(ctx):
    bad(ctx, "end_hour", start_hour=8, end_hour=10)


def test_paired_without_class(ctx):
    bad(ctx, "paired_class", paired=True)


def test_paired_class_without_paired(ctx):
    bad(ctx, "paired_class", paired=False, paired_class=ctx.cls2)


def test_paired_class_equals_class(ctx):
    bad(ctx, "paired_class", paired=True, paired_class=ctx.cls)


def test_class_other_school(ctx, other):
    bad(ctx, "school_class", school_class=other.cls)


def test_paired_class_other_school(ctx, other):
    bad(ctx, "paired_class", paired=True, paired_class=other.cls2)


def test_teacher_other_school(ctx, other):
    bad(ctx, "teacher", teacher=other.teacher)


def test_subject_other_school(ctx, other):
    bad(ctx, "subject", subject=other.subject)


@pytest.mark.parametrize(
    "kw",
    [
        dict(day="SABATO"),
        dict(start_hour=7, end_hour=8),
        dict(start_hour=14, end_hour=15),
        dict(start_hour=8, end_hour=10),
        dict(start_hour=13, end_hour=15),
        dict(paired=True),
        dict(paired=False, paired_class_id=-1),
    ],
)
def test_db_rejects_bypassing_clean(ctx, kw):
    if kw.get("paired_class_id") == -1:
        kw = dict(kw, paired_class=ctx.cls2)
        kw.pop("paired_class_id")
    with pytest.raises(IntegrityError), transaction.atomic():
        ctx.slot(**kw).save()


def test_db_rejects_paired_class_equal_class(ctx):
    with pytest.raises(IntegrityError), transaction.atomic():
        ctx.slot(paired=True, paired_class=ctx.cls).save()


def test_double_booking_allowed(ctx):
    other_teacher = Teacher.objects.create(
        school=ctx.school, name="BIANCHI", type="ASSUNTO"
    )
    ctx.slot().save()
    ctx.slot(teacher=other_teacher).save()  # same class, day, hour
    ctx.slot(school_class=ctx.cls2).save()  # same teacher, day, hour
    assert Slot.objects.count() == 3


def test_cascade_on_timetable_delete(ctx):
    ctx.slot().save()
    ctx.tt.delete()
    assert Slot.objects.count() == 0


@pytest.mark.parametrize("which", ["cls", "teacher", "subject"])
def test_protect_referenced(ctx, which):
    ctx.slot().save()
    with pytest.raises(ProtectedError):
        getattr(ctx, which).delete()


def test_protect_paired_only_class(ctx):
    ctx.slot(paired=True, paired_class=ctx.cls2).save()
    with pytest.raises(ProtectedError):
        ctx.cls2.delete()
