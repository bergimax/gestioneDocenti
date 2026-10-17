from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.auth.models import PermissionsMixin
from django.db import models
from django.db.models.functions import Lower


class Role(models.TextChoices):
    ADMIN = "ADMIN", "Amministratore"
    SEGRETERIA = "SEGRETERIA", "Segreteria"


class UserManager(BaseUserManager):
    use_in_migrations = True

    def get_by_natural_key(self, email):
        return self.get(email__iexact=email.strip())

    def create_user(self, email, password, role):
        user = self.model(email=email, role=role)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password, role=Role.ADMIN, **extra):
        user = self.model(email=email, role=role, is_superuser=True)
        user.set_password(password)
        user.save(using=self._db)
        return user


class User(AbstractBaseUser, PermissionsMixin):
    email = models.EmailField("email", unique=True)
    role = models.CharField("ruolo", max_length=20, choices=Role.choices)
    is_active = models.BooleanField("attivo", default=True)

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["role"]

    class Meta:
        constraints = [
            models.UniqueConstraint(Lower("email"), name="accounts_user_email_ci"),
            models.CheckConstraint(
                condition=models.Q(role__in=Role.values), name="accounts_user_role"
            ),
        ]

    @property
    def is_staff(self):
        # Django admin only reads is_staff; access follows the superuser flag.
        return self.is_active and self.is_superuser

    def save(self, *args, **kwargs):
        self.email = self.email.strip().lower()
        super().save(*args, **kwargs)

    def __str__(self):
        return self.email


class LoginFailure(models.Model):
    """One failed login attempt, kept in Postgres for the login throttle."""

    email = models.CharField(max_length=254, db_index=True)
    client_address = models.CharField(max_length=64, db_index=True)
    attempted_at = models.DateTimeField(db_index=True)

    def __str__(self):
        return f"{self.email} {self.client_address} {self.attempted_at}"
