from django.contrib.auth.views import LogoutView
from django.urls import path

from accounts import views

urlpatterns = [
    path("login/", views.Login.as_view(), name="login"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("password/", views.ChangePassword.as_view(), name="password_change"),
]
