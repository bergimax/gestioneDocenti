from django.conf import settings
from django.contrib import messages
from django.contrib.auth.views import LoginView, PasswordChangeView
from django.urls import reverse_lazy

from accounts import throttle
from accounts.forms import ChangePasswordForm, LoginForm

THROTTLE_MESSAGE = "Troppi tentativi di accesso. Riprova tra qualche minuto."


class Login(LoginView):
    form_class = LoginForm
    template_name = "accounts/login.html"
    redirect_authenticated_user = True

    def _address(self):
        return self.request.META.get("REMOTE_ADDR", "")

    def post(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            return super().post(request, *args, **kwargs)
        if throttle.is_locked(request.POST.get("username", ""), self._address()):
            context = self.get_context_data(
                form=self.form_class(request=request),
                throttled=True,
                throttle_message=THROTTLE_MESSAGE,
            )
            response = self.render_to_response(context)
            response.status_code = 429
            response["Retry-After"] = str(settings.LOGIN_WINDOW_SECONDS)
            return response
        return super().post(request, *args, **kwargs)

    def form_valid(self, form):
        throttle.clear_account(form.cleaned_data["username"])
        return super().form_valid(form)

    def form_invalid(self, form):
        username = self.request.POST.get("username", "")
        if throttle.normalize_email(username) and self.request.POST.get("password"):
            throttle.record_failure(username, self._address())
        return super().form_invalid(form)


class ChangePassword(PasswordChangeView):
    form_class = ChangePasswordForm
    template_name = "accounts/password_change.html"
    success_url = reverse_lazy("password_change")

    def form_valid(self, form):
        response = super().form_valid(form)  # keeps this session via session hash
        messages.success(self.request, "Password modificata.")
        return response
