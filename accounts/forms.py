from django import forms
from django.contrib.auth.forms import AuthenticationForm, PasswordChangeForm

GENERIC_LOGIN_ERROR = "Email o password non corretti."


class LoginForm(AuthenticationForm):
    error_messages = {
        "invalid_login": GENERIC_LOGIN_ERROR,
        "inactive": GENERIC_LOGIN_ERROR,
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["username"] = forms.CharField(
            label="Email", widget=forms.EmailInput(attrs={"autofocus": True})
        )
        self.fields["password"].label = "Password"

    def clean_username(self):
        return self.cleaned_data["username"].strip().lower()


class ChangePasswordForm(PasswordChangeForm):
    error_messages = {
        **PasswordChangeForm.error_messages,
        "password_incorrect": "La password attuale non è corretta.",
        "password_mismatch": "Le due password non coincidono.",
        "same_password": "La nuova password deve essere diversa da quella attuale.",
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["old_password"].label = "Password attuale"
        self.fields["new_password1"].label = "Nuova password"
        self.fields["new_password1"].help_text = "Almeno 8 caratteri."
        self.fields["new_password2"].label = "Conferma nuova password"
        for field in self.fields.values():
            field.error_messages["required"] = "Questo campo è obbligatorio."

    def clean_new_password1(self):
        password = self.cleaned_data["new_password1"]
        if self.user.check_password(password):
            raise forms.ValidationError(
                self.error_messages["same_password"], code="same_password"
            )
        return password
