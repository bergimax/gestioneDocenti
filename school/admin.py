from django.contrib import admin

from .models import (
    Assignment,
    CalendarEntry,
    Class,
    ClassPairing,
    School,
    Subject,
    Teacher,
)

admin.site.register(
    [School, Class, Teacher, Subject, Assignment, ClassPairing, CalendarEntry]
)
