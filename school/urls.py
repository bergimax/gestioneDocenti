from django.urls import path

from .views import (
    assignments,
    calendar,
    classes,
    hub,
    pairings,
    setup,
    subjects,
    teachers,
)

urlpatterns = [
    path("setup/", setup.setup_page, name="setup"),
    path("setup/upload/<str:file_type>/", setup.setup_upload, name="setup_upload"),
    path("setup/validate/", setup.setup_validate, name="setup_validate"),
    path("setup/correction/", setup.setup_correction, name="setup_correction"),
    path("setup/add-date/", setup.setup_add_date, name="setup_add_date"),
    path("setup/save/", setup.setup_save, name="setup_save"),
    path("setup/completato/", setup.setup_done, name="setup_done"),
    path("dati-scuola/", hub.school_data, name="school_data"),
    path("dati-scuola/docenti/", teachers.teacher_list, name="teacher_list"),
    path("dati-scuola/docenti/nuovo/", teachers.teacher_create, name="teacher_create"),
    path(
        "dati-scuola/docenti/<int:pk>/modifica/",
        teachers.teacher_edit,
        name="teacher_edit",
    ),
    path(
        "dati-scuola/docenti/<int:pk>/elimina/",
        teachers.teacher_delete,
        name="teacher_delete",
    ),
    path("dati-scuola/classi/", classes.class_list, name="class_list"),
    path("dati-scuola/classi/nuova/", classes.class_create, name="class_create"),
    path(
        "dati-scuola/classi/<int:pk>/modifica/",
        classes.class_edit,
        name="class_edit",
    ),
    path(
        "dati-scuola/classi/<int:pk>/elimina/",
        classes.class_delete,
        name="class_delete",
    ),
    path("dati-scuola/materie/", subjects.subject_list, name="subject_list"),
    path("dati-scuola/materie/nuova/", subjects.subject_create, name="subject_create"),
    path(
        "dati-scuola/materie/<int:pk>/modifica/",
        subjects.subject_edit,
        name="subject_edit",
    ),
    path(
        "dati-scuola/materie/<int:pk>/elimina/",
        subjects.subject_delete,
        name="subject_delete",
    ),
    path(
        "dati-scuola/assegnazioni/",
        assignments.assignment_list,
        name="assignment_list",
    ),
    path(
        "dati-scuola/assegnazioni/nuova/",
        assignments.assignment_create,
        name="assignment_create",
    ),
    path(
        "dati-scuola/assegnazioni/<int:pk>/modifica/",
        assignments.assignment_edit,
        name="assignment_edit",
    ),
    path(
        "dati-scuola/assegnazioni/<int:pk>/elimina/",
        assignments.assignment_delete,
        name="assignment_delete",
    ),
    path("dati-scuola/accoppiamenti/", pairings.pairing_list, name="pairing_list"),
    path(
        "dati-scuola/accoppiamenti/nuovo/",
        pairings.pairing_create,
        name="pairing_create",
    ),
    path(
        "dati-scuola/accoppiamenti/<int:pk>/modifica/",
        pairings.pairing_edit,
        name="pairing_edit",
    ),
    path(
        "dati-scuola/accoppiamenti/<int:pk>/elimina/",
        pairings.pairing_delete,
        name="pairing_delete",
    ),
    path("dati-scuola/calendario/", calendar.calendar_month, name="calendar_month"),
    path(
        "dati-scuola/calendario/nuova/",
        calendar.calendar_create,
        name="calendar_create",
    ),
    path(
        "dati-scuola/calendario/<int:pk>/modifica/",
        calendar.calendar_edit,
        name="calendar_edit",
    ),
    path(
        "dati-scuola/calendario/<int:pk>/elimina/",
        calendar.calendar_delete,
        name="calendar_delete",
    ),
]
