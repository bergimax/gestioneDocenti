from django.urls import path

from . import views

urlpatterns = [
    path("disponibilita/", views.availability_page, name="availability"),
    path(
        "disponibilita/<str:week>/copia/",
        views.availability_copy,
        name="availability_copy",
    ),
    path(
        "disponibilita/<str:week>/docente/<int:teacher_id>/toggle/",
        views.availability_toggle,
        name="availability_toggle",
    ),
    path(
        "disponibilita/<str:week>/docente/<int:teacher_id>/salva/",
        views.availability_save,
        name="availability_save",
    ),
]
