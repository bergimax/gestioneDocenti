from django.contrib import messages
from django.contrib.auth.views import LoginView, PasswordChangeView
from django.urls import reverse_lazy

from accounts.forms import ChangePasswordForm, LoginForm


class Login(LoginView):
    form_class = LoginForm
    template_name = "accounts/login.html"
    redirect_authenticated_user = True


class ChangePassword(PasswordChangeView):
    form_class = ChangePasswordForm
    template_name = "accounts/password_change.html"
    success_url = reverse_lazy("password_change")

    def form_valid(self, form):
        response = super().form_valid(form)  # keeps this session via session hash
        messages.success(self.request, "Password modificata.")
        return response
