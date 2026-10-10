"""Fixtures shared by the timetable edit tests."""

import datetime

import pytest
from django.test import Client

from accounts.models import User
from scheduling.models import Slot, Timetable
from school.models import (
    Assignment,
    CalendarEntry,
    Class,
    School,
    Subject,
    Teacher,
)

D = datetime.date
MON = D(2026, 10, 12)


def login(role):
    user = User.objects.create_user(
        f"{role.lower()}@example.com", "pw-long-enough", role
    )
    client = Client()
    client.force_login(user)
    return client


@pytest.fixture
def admin(db):
    return login("ADMIN")


@pytest.fixture
def seg(db):
    return login("SEGRETERIA")


@pytest.fixture
def w(db):
    """Two classes, Monday and Tuesday of 4 hours, the other days closed.

    Every class has two lessons a day (8 and 9), so 10-13 are free. Rossi and
    Bianchi have more hours than they are placed for: the solver would fill
    the free hours if it were allowed to.
    """

    class W:
        pass

    w = W()
    s = w.school = School.objects.create(
        name="S",
        formative_year="2026-2027",
        year_start=D(2026, 9, 1),
        year_end=D(2027, 6, 30),
    )
    for i in range(5):
        CalendarEntry.objects.create(
            school=s,
            date=MON + datetime.timedelta(days=i),
            max_hours=4 if i < 2 else 0,
            is_closure=i >= 2,
        )
    w.a = Class.objects.create(school=s, name="1A")
    w.b = Class.objects.create(school=s, name="1B")
    w.rossi = Teacher.objects.create(
        school=s, name="ROSSI MARIO", type="ASSUNTO", first_time=False
    )
    w.bianchi = Teacher.objects.create(
        school=s, name="BIANCHI ANNA", type="ASSUNTO", first_time=False
    )
    w.mat = Subject.objects.create(
        school=s, name="Matematica", kind="TEORIA", cognitive_weight="MEDIO"
    )
    w.ita = Subject.objects.create(
        school=s, name="Italiano", kind="TEORIA", cognitive_weight="MEDIO"
    )
    w.lab = Subject.objects.create(
        school=s, name="Laboratorio", kind="PRATICA", cognitive_weight="MEDIO"
    )
    w.asg = {}
    for name, teacher, klass, subject in (
        ("a_mat", w.rossi, w.a, w.mat),
        ("a_ita", w.bianchi, w.a, w.ita),
        ("b_mat", w.rossi, w.b, w.mat),
        ("b_ita", w.bianchi, w.b, w.ita),
        ("a_lab", w.bianchi, w.a, w.lab),
    ):
        w.asg[name] = Assignment.objects.create(
            school=s,
            teacher=teacher,
            school_class=klass,
            subject=subject,
            ore_totali=4,
        )
    w.tt = Timetable.objects.create(school=s, week_start=MON, version=1)

    def slot(klass, teacher, subject, day, hour, **kw):
        return Slot.objects.create(
            timetable=w.tt,
            school_class=klass,
            teacher=teacher,
            subject=subject,
            day=day,
            start_hour=hour,
            end_hour=hour + 1,
            **kw,
        )

    w.slot = slot
    w.slots = {}
    for day in ("LUNEDI", "MARTEDI"):
        w.slots[day, "a_mat"] = w.slot(w.a, w.rossi, w.mat, day, 8)
        w.slots[day, "a_ita"] = w.slot(w.a, w.bianchi, w.ita, day, 9)
        w.slots[day, "b_ita"] = w.slot(w.b, w.bianchi, w.ita, day, 8)
        w.slots[day, "b_mat"] = w.slot(w.b, w.rossi, w.mat, day, 9)
    return w
