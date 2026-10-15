from django.urls import path

from export.views import export_excel, export_pdf

from . import audit_views, instruction_views, views

urlpatterns = [
    path("audit/", audit_views.audit_page, name="audit_log"),
    path("pesi/", views.weights_page, name="soft_weights"),
    path("pesi/applica/", views.weights_apply, name="soft_weights_apply"),
    path("pesi/ripristina/", views.weights_reset, name="soft_weights_reset"),
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
    path(
        "orario/<str:week>/deroga/",
        views.timetable_quick_action,
        name="timetable_quick_action",
    ),
    path(
        "orario/<str:week>/istruzione/",
        instruction_views.instruction_create,
        name="instruction_create",
    ),
    path(
        "orario/<str:week>/istruzione/<int:draft_id>/conferma/",
        instruction_views.instruction_confirm,
        name="instruction_confirm",
    ),
    path(
        "orario/<str:week>/istruzione/<int:draft_id>/scarta/",
        instruction_views.instruction_discard,
        name="instruction_discard",
    ),
    path(
        "orario/<str:week>/sostituzione/",
        views.timetable_repair,
        name="timetable_repair",
    ),
    path(
        "orario/<str:week>/sostituzione/stato/",
        views.timetable_repair_status,
        name="timetable_repair_status",
    ),
    path(
        "orario/<str:week>/sostituzione/<int:job_id>/conferma/",
        views.timetable_repair_confirm,
        name="timetable_repair_confirm",
    ),
    path(
        "orario/<str:week>/sostituzione/<int:job_id>/annulla/",
        views.timetable_repair_dismiss,
        name="timetable_repair_dismiss",
    ),
    path(
        "orario/<str:week>/alternative/<int:job_id>/<int:index>/scegli/",
        views.timetable_alt_choose,
        name="timetable_alt_choose",
    ),
    path(
        "orario/<str:week>/alternative/<int:job_id>/<int:index>/griglia/",
        views.timetable_alt_view,
        name="timetable_alt_view",
    ),
    path(
        "orario/<str:week>/approva/", views.timetable_approve, name="timetable_approve"
    ),
    path(
        "orario/<str:week>/annulla-approvazione/",
        views.timetable_undo_approval,
        name="timetable_undo_approval",
    ),
    path("orario/<str:week>/export-pdf/", export_pdf, name="timetable_export_pdf"),
    path(
        "orario/<str:week>/export-excel/",
        export_excel,
        name="timetable_export_excel",
    ),
    path("orario/<str:week>/stato/", views.timetable_job, name="timetable_job"),
]
