from django.urls import path

from .views import assignments, classes, hub, pairings, subjects, teachers

urlpatterns = [
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
]
