from django.urls import path

from . import views

urlpatterns = [
    path("feeds/", views.feeds_page, name="feeds"),
    path(
        "feeds/rigenera/<str:kind>/<int:pk>/",
        views.feed_regenerate,
        name="feed_regenerate",
    ),
    path("feeds/<str:token>.ics", views.feed_ics, name="feed_ics"),
]
