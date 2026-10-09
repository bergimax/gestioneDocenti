from django.core.exceptions import ValidationError

MIN_LENGTH = 8


class MinimumLengthItalian:
    """Minimum length with a fixed Italian message (independent of the locale)."""

    def validate(self, password, user=None):
        if len(password) < MIN_LENGTH:
            raise ValidationError(
                f"La password è troppo corta: servono almeno {MIN_LENGTH} caratteri.",
                code="password_too_short",
            )

    def get_help_text(self):
        return f"La password deve avere almeno {MIN_LENGTH} caratteri."
