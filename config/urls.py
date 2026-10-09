from django.http import HttpResponse
from django.urls import include, path


def home(request):
    return HttpResponse(
        "<!doctype html><html lang='it'><title>Gestione docenti</title>"
        "<h1>Gestione docenti</h1>"
    )


urlpatterns = [
    path("", home, name="home"),
    path("", include("accounts.urls")),
]
