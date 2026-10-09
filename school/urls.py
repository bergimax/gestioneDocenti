from django.urls import path

from .views import hub, teachers

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
]
