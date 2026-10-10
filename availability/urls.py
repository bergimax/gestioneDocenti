from django.urls import path

from . import views

urlpatterns = [
    path("l/<path:token>/salva/", views.link_save, name="availability_link_save"),
    path("l/<path:token>/", views.link_open, name="availability_link_open"),
    path("disponibilita/link/", views.link_list, name="availability_links"),
    path(
        "disponibilita/link/<int:link_id>/revoca/",
        views.link_revoke,
        name="availability_link_revoke",
    ),
    path(
        "disponibilita/<str:week>/docente/<int:teacher_id>/link/",
        views.link_generate,
        name="availability_link_generate",
    ),
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
