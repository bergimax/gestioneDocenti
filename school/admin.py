from django.contrib import admin

from .models import Class, School, Subject, Teacher

admin.site.register([School, Class, Teacher, Subject])
