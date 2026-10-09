from django.contrib import admin

from .models import Assignment, Class, ClassPairing, School, Subject, Teacher

admin.site.register([School, Class, Teacher, Subject, Assignment, ClassPairing])
