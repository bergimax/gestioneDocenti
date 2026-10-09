import datetime

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from django.test import Client

from accounts.models import User
from school.models import Assignment, Class, ClassPairing, School, Subject, Teacher


def make_school(name):
    return School.objects.create(
        name=name,
        formative_year="2026-2027",
        year_start=datetime.date(2026, 9, 1),
        year_end=datetime.date(2027, 6, 30),
    )


class World:
    """Teacher, two classes and a subject for one school."""

    def __init__(self, school):
        self.school = school
        self.teacher = Teacher.objects.create(
            school=school, name="ROSSI MARIO", type="ASSUNTO"
        )
        self.teacher2 = Teacher.objects.create(
            school=school, name="BIANCHI LUCA", type="CONTRATTO"
        )
        self.c1 = Class.objects.create(school=school, name="1A")
        self.c2 = Class.objects.create(school=school, name="1B")
        self.subject = Subject.objects.create(
            school=school, name="Italiano", kind="TEORIA", cognitive_weight="ALTO"
        )
        self.subject2 = Subject.objects.create(
            school=school, name="Storia", kind="TEORIA", cognitive_weight="MEDIO"
        )

    def assignment(self, **kw):
        kw.setdefault("school", self.school)
        kw.setdefault("teacher", self.teacher)
        kw.setdefault("school_class", self.c1)
        kw.setdefault("subject", self.subject)
        kw.setdefault("ore_totali", 40)
        return Assignment(**kw)

    def pairing(self, **kw):
        kw.setdefault("school", self.school)
        kw.setdefault("class_a", self.c1)
        kw.setdefault("class_b", self.c2)
        kw.setdefault("subject", self.subject)
        kw.setdefault("teacher", self.teacher)
        return ClassPairing(**kw)


@pytest.fixture
def w(db):
    return World(make_school("Scuola A"))


@pytest.fixture
def w2(db):
    return World(make_school("Scuola B"))


def insert_fails(obj):
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            obj.save()


# Assignment


def test_assignment_valid_passes_full_clean(w):
    a = w.assignment()
    a.full_clean()
    a.save()
    assert str(a) == "ROSSI MARIO - 1A - Italiano"


def test_assignment_negative_hours_rejected(w):
    with pytest.raises(ValidationError) as e:
        w.assignment(ore_totali=-1).full_clean()
    assert "ore_totali" in e.value.message_dict
    w.assignment(ore_totali=0).full_clean()


def test_assignment_unique_per_school_teacher_class_subject(w):
    w.assignment().save()
    insert_fails(w.assignment())
    w.assignment(school_class=w.c2).save()
    w.assignment(subject=w.subject2).save()


@pytest.mark.parametrize("field", ["teacher", "school_class", "subject"])
def test_assignment_same_school_check(w, w2, field):
    other = {"teacher": w2.teacher, "school_class": w2.c1, "subject": w2.subject}
    with pytest.raises(ValidationError) as e:
        w.assignment(**{field: other[field]}).full_clean()
    assert field in e.value.message_dict


def test_assignment_missing_fields_reported_without_crash(w):
    with pytest.raises(ValidationError) as e:
        Assignment(school=w.school).full_clean()
    assert {"teacher", "school_class", "subject", "ore_totali"} <= set(
        e.value.message_dict
    )


def test_assignment_school_protected(w):
    w.assignment().save()
    with pytest.raises(ProtectedError):
        w.school.delete()


@pytest.mark.parametrize("field", ["teacher", "school_class", "subject"])
def test_assignment_references_protected(w, field):
    a = w.assignment()
    a.save()
    with pytest.raises(ProtectedError):
        getattr(a, field).delete()


# Pairing


def test_pairing_valid_passes_full_clean(w):
    p = w.pairing(note="")
    p.full_clean()
    p.save()
    assert str(p) == "1A + 1B - Italiano"


def test_pairing_without_teacher_passes(w):
    p = w.pairing(teacher=None)
    p.full_clean()
    p.save()
    assert str(p)


@pytest.mark.parametrize("field", ["class_a", "class_b", "subject", "teacher"])
def test_pairing_same_school_check(w, w2, field):
    other = {
        "class_a": w2.c1,
        "class_b": w2.c2,
        "subject": w2.subject,
        "teacher": w2.teacher,
    }
    with pytest.raises(ValidationError) as e:
        w.pairing(**{field: other[field]}).full_clean()
    assert field in e.value.message_dict


def test_pairing_class_with_itself_rejected_by_db(w):
    insert_fails(w.pairing(class_b=w.c1))


def test_pairing_reverse_order_rejected_by_db(w):
    insert_fails(w.pairing(class_a=w.c2, class_b=w.c1))


@pytest.mark.parametrize("with_teacher", [True, False])
def test_pairing_unique_once_per_school(w, with_teacher):
    teacher = w.teacher if with_teacher else None
    w.pairing(teacher=teacher).save()
    insert_fails(w.pairing(teacher=teacher))
    # same pair in the other order is the same pairing: refused too
    insert_fails(w.pairing(teacher=teacher, class_a=w.c2, class_b=w.c1))


def test_pairing_different_teachers_allowed(w):
    w.pairing(teacher=w.teacher).save()
    w.pairing(teacher=w.teacher2).save()
    w.pairing(teacher=None).save()


def test_pairing_same_looking_pairs_in_two_schools_allowed(w, w2):
    w.pairing().save()
    w2.pairing().save()
    assert ClassPairing.objects.count() == 2


def test_pairing_school_protected(w):
    w.pairing().save()
    with pytest.raises(ProtectedError):
        w.school.delete()


@pytest.mark.parametrize("field", ["class_a", "class_b", "subject", "teacher"])
def test_pairing_references_protected(w, field):
    p = w.pairing()
    p.save()
    with pytest.raises(ProtectedError):
        getattr(p, field).delete()


# Honest limit: same-school is model-level only


def test_mismatched_rows_saved_without_full_clean_are_not_rejected_by_db(w, w2):
    a = Assignment.objects.create(
        school=w.school,
        teacher=w2.teacher,
        school_class=w2.c1,
        subject=w2.subject,
        ore_totali=10,
    )
    p = ClassPairing.objects.create(
        school=w.school, class_a=w2.c1, class_b=w2.c2, subject=w2.subject
    )
    assert Assignment.objects.filter(pk=a.pk).exists()
    assert ClassPairing.objects.filter(pk=p.pk).exists()
    with pytest.raises(ValidationError):
        a.full_clean()
    with pytest.raises(ValidationError):
        p.full_clean()


# Admin


def test_admin_lists_new_models(w):
    User.objects.create_superuser("root@example.com", "pw-correct-1")
    c = Client()
    c.post(
        "/admin/login/?next=/admin/",
        {"username": "root@example.com", "password": "pw-correct-1"},
    )
    r = c.get("/admin/")
    assert r.status_code == 200
    content = r.content.decode()
    for label in ("Assignments", "Class pairings"):
        assert label in content
    assert c.get("/admin/school/assignment/").status_code == 200
    assert c.get("/admin/school/classpairing/").status_code == 200
