from django.contrib import admin
from django.urls import include, path

from config import views

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", views.home, name="home"),
    path("partials/ping/", views.overview_ping, name="overview_ping"),
    path("disponibilita/", views.availability, name="availability"),
    path("orario/", views.timetable, name="timetable"),
    path("", include("school.urls")),
    path("", include("accounts.urls")),
]
