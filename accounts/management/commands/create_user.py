import getpass

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.core.validators import validate_email
from django.db import transaction

from accounts.models import Role


class Command(BaseCommand):
    help = "Create a user with a role, or reset password and role if it exists."

    def add_arguments(self, parser):
        parser.add_argument("email")
        parser.add_argument("--role", required=True, help="ADMIN or SEGRETERIA")
        parser.add_argument("--password", help="asked interactively if omitted")

    def handle(self, *args, email, role, password, **options):
        email = email.strip().lower()
        try:
            validate_email(email)
        except ValidationError:
            raise CommandError(f"Invalid email: {email}") from None
        if role not in Role.values:
            raise CommandError(f"Invalid role {role!r}: use ADMIN or SEGRETERIA.")
        if password is None:
            password = getpass.getpass("Password: ")
            if getpass.getpass("Password (again): ") != password:
                raise CommandError("The two passwords do not match.")
        try:
            validate_password(password)
        except ValidationError as exc:
            raise CommandError(" ".join(exc.messages)) from None

        User = get_user_model()
        with transaction.atomic():
            user = User.objects.filter(email__iexact=email).first()
            if user is None:
                User.objects.create_user(email, password, role)
                self.stdout.write(f"Created user {email} with role {role}.")
            else:
                user.role = role
                user.set_password(password)
                user.save()
                self.stdout.write(
                    f"User {email} already existed: password and role reset "
                    f"(role {role})."
                )
