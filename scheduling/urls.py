from django.urls import path

from . import views

urlpatterns = [
    path("orario/", views.timetable_page, name="timetable"),
    path("orario/<str:week>/griglia/", views.timetable_grid, name="timetable_grid"),
    path(
        "orario/<str:week>/genera/", views.timetable_generate, name="timetable_generate"
    ),
    path("orario/<str:week>/sposta/", views.timetable_move, name="timetable_move"),
    path(
        "orario/<str:week>/assegnabili/<int:class_id>/",
        views.timetable_assignable,
        name="timetable_assignable",
    ),
    path("orario/<str:week>/assegna/", views.timetable_assign, name="timetable_assign"),
    path(
        "orario/<str:week>/conflitti/approva/",
        views.timetable_conflicts_approve,
        name="timetable_conflicts_approve",
    ),
    path(
        "orario/<str:week>/conflitti/rifiuta/",
        views.timetable_conflicts_reject,
        name="timetable_conflicts_reject",
    ),
    path("orario/<str:week>/stato/", views.timetable_job, name="timetable_job"),
]
