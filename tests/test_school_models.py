import datetime

import pytest
from django.contrib import admin
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import Client

from accounts.models import User
from school.models import Class, School, Subject, Teacher


@pytest.fixture
def school(db):
    return School.objects.create(
        name="Scuola A",
        formative_year="2026-2027",
        year_start=datetime.date(2026, 9, 1),
        year_end=datetime.date(2027, 6, 30),
    )


@pytest.fixture
def other_school(db):
    return School.objects.create(
        name="Scuola B",
        formative_year="2026-2027",
        year_start=datetime.date(2026, 9, 1),
        year_end=datetime.date(2027, 6, 30),
    )


def make_teacher(school, name="ROSSI MARIO", **kw):
    kw.setdefault("type", "ASSUNTO")
    return Teacher(school=school, name=name, **kw)


def make_subject(school, name="Italiano", **kw):
    kw.setdefault("kind", "TEORIA")
    kw.setdefault("cognitive_weight", "ALTO")
    return Subject(school=school, name=name, **kw)


@pytest.mark.parametrize(
    "obj_factory, field",
    [
        (lambda s: make_teacher(s, type="BOSS"), "type"),
        (lambda s: make_subject(s, kind="BOSS"), "kind"),
        (lambda s: make_subject(s, cognitive_weight="BOSS"), "cognitive_weight"),
        (lambda s: Class(school=s, name="1A", group="BOSS"), "group"),
    ],
)
def test_invalid_choice_rejected(school, obj_factory, field):
    with pytest.raises(ValidationError) as exc:
        obj_factory(school).full_clean()
    assert field in exc.value.message_dict


@pytest.mark.parametrize("t", ["ASSUNTO", "CONTRATTO"])
def test_teacher_types_valid(school, t):
    make_teacher(school, type=t).full_clean()


@pytest.mark.parametrize("k", ["TEORIA", "PRATICA"])
@pytest.mark.parametrize("w", ["ALTO", "MEDIO", "BASSO"])
def test_subject_choices_valid(school, k, w):
    make_subject(school, kind=k, cognitive_weight=w).full_clean()


@pytest.mark.parametrize("g", ["PRIME", "SECONDE", "TERZE", "QUARTE", "PRIMA4+2"])
def test_class_groups_valid(school, g):
    Class(school=school, name="1A", group=g).full_clean()


def test_defaults(school):
    c = Class.objects.create(school=school, name="1A")
    t = Teacher.objects.create(school=school, name="ROSSI MARIO", type="ASSUNTO")
    assert c.student_count == 0
    assert t.active is True
    assert t.first_time is True


def test_required_fields(school):
    with pytest.raises(ValidationError) as exc:
        Teacher(school=school, name="X Y").full_clean()
    assert "type" in exc.value.message_dict
    with pytest.raises(ValidationError) as exc:
        Subject(school=school, name="Mat").full_clean()
    assert {"kind", "cognitive_weight"} <= set(exc.value.message_dict)
    with pytest.raises(IntegrityError), transaction.atomic():
        Teacher.objects.create(school=school, name="X Y", type=None)
    with pytest.raises(IntegrityError), transaction.atomic():
        Subject.objects.create(
            school=school, name="Mat", kind=None, cognitive_weight=None
        )


def test_class_group_optional(school):
    c = Class(school=school, name="1A")
    c.full_clean()
    c.save()
    assert Class.objects.get(pk=c.pk).group == ""


def test_negative_student_count_rejected(school):
    with pytest.raises(ValidationError) as exc:
        Class(school=school, name="1A", student_count=-1).full_clean()
    assert "student_count" in exc.value.message_dict


@pytest.mark.parametrize("end", [datetime.date(2026, 9, 1), datetime.date(2026, 8, 31)])
def test_school_year_end_after_start(end):
    s = School(
        name="S",
        formative_year="2026-2027",
        year_start=datetime.date(2026, 9, 1),
        year_end=end,
    )
    with pytest.raises(ValidationError) as exc:
        s.full_clean()
    assert "year_end" in exc.value.message_dict


def test_school_year_order_enforced_by_db(db):
    with pytest.raises(IntegrityError), transaction.atomic():
        School.objects.create(
            name="S",
            formative_year="x",
            year_start=datetime.date(2026, 9, 1),
            year_end=datetime.date(2026, 9, 1),
        )


def test_class_name_unique_per_school(school, other_school):
    Class.objects.create(school=school, name="1A")
    with pytest.raises(IntegrityError), transaction.atomic():
        Class.objects.create(school=school, name="1A")
    Class.objects.create(school=other_school, name="1A")


def test_teacher_name_unique_per_school(school, other_school):
    make_teacher(school).save()
    with pytest.raises(IntegrityError), transaction.atomic():
        make_teacher(school).save()
    make_teacher(other_school).save()


def test_teacher_name_case_insensitive(school):
    make_teacher(school, "ROSSI MARIO").save()
    with pytest.raises(IntegrityError), transaction.atomic():
        make_teacher(school, "Rossi Mario").save()


def test_subject_name_unique_per_school(school, other_school):
    make_subject(school).save()
    with pytest.raises(IntegrityError), transaction.atomic():
        make_subject(school).save()
    make_subject(other_school).save()


def test_str(school):
    assert str(school) == "Scuola A"
    assert str(Class(school=school, name="1A")) == "1A"
    assert str(make_teacher(school)) == "ROSSI MARIO"
    assert str(make_subject(school)) == "Italiano"


def test_admin_lists_and_opens_all_models(school, monkeypatch):
    # User has no is_staff (accounts is out of scope here), so the default
    # admin permission check cannot pass; superuser role is checked instead.
    monkeypatch.setattr(
        admin.site,
        "has_permission",
        lambda request: request.user.is_active and request.user.is_superuser,
    )
    root = User.objects.create_user("root@example.com", "pw-correct-1", "ADMIN")
    root.is_superuser = True
    root.save()
    c = Client()
    c.force_login(root)
    objs = {
        "school": school,
        "class": Class.objects.create(school=school, name="1A"),
        "teacher": make_teacher(school),
        "subject": make_subject(school),
    }
    objs["teacher"].save()
    objs["subject"].save()
    for model, obj in objs.items():
        assert c.get(f"/admin/school/{model}/").status_code == 200
        assert c.get(f"/admin/school/{model}/{obj.pk}/change/").status_code == 200
