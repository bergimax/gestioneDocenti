from django.urls import path

from . import views

urlpatterns = [
    path("orario/", views.timetable_page, name="timetable"),
    path("orario/<str:week>/griglia/", views.timetable_grid, name="timetable_grid"),
    path(
        "orario/<str:week>/genera/", views.timetable_generate, name="timetable_generate"
    ),
    path("orario/<str:week>/sposta/", views.timetable_move, name="timetable_move"),
    path("orario/<str:week>/stato/", views.timetable_job, name="timetable_job"),
]
