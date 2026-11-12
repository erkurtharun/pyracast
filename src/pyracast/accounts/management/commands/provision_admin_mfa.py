from typing import Any
from urllib.parse import quote, urlencode

from allauth.account.models import EmailAddress
from allauth.mfa.models import Authenticator
from allauth.mfa.totp.internal.auth import generate_totp_secret
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.core.validators import validate_email
from django.db import transaction
from django.views.decorators.debug import sensitive_variables

from pyracast.accounts.audit import record_security_event
from pyracast.accounts.mfa import MFAAdapter


def bind_administrator_email(user: User, supplied_email: str | None) -> None:
    existing = EmailAddress.objects.filter(user=user, primary=True).first()
    if supplied_email is None:
        if existing is None:
            raise CommandError(
                "--email is required to bind a legacy administrator's email identity."
            )
        supplied_email = existing.email
    email = supplied_email.strip().lower()
    try:
        validate_email(email)
    except ValidationError:
        raise CommandError("A valid email address is required.") from None
    if len(email) > 254:
        raise CommandError("A valid email address is required.")
    if existing is not None and existing.email.lower() != email:
        raise CommandError("An email identity already exists; provisioning never replaces it.")
    if (
        EmailAddress.objects.filter(email__iexact=email).exclude(user=user).exists()
        or User.objects.filter(email__iexact=email).exclude(pk=user.pk).exists()
    ):
        raise CommandError("The email address is already associated with another account.")
    if existing is None:
        if EmailAddress.objects.filter(user=user).exists():
            raise CommandError("Resolve the existing email identities before provisioning.")
        EmailAddress.objects.create(user=user, email=email, primary=True, verified=False)
    user.email = email
    user.save(update_fields=["email"])


class Command(BaseCommand):
    help = "Provision an administrator TOTP factor through the trusted operator channel."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--username", required=True)
        parser.add_argument("--email", help="Explicit email identity for a legacy administrator.")

    @sensitive_variables()
    def handle(self, *args: Any, **options: Any) -> None:
        with transaction.atomic():
            user = User.objects.select_for_update().filter(username=options["username"]).first()
            if user is None or not user.is_active or not (user.is_staff or user.is_superuser):
                raise CommandError("An active administrator account is required.")
            if Authenticator.objects.filter(user=user).exists():
                raise CommandError(
                    "An authenticator already exists; provisioning never replaces a factor."
                )
            bind_administrator_email(user, options["email"])
            secret = generate_totp_secret()
            Authenticator.objects.create(
                user=user, type="totp", data={"secret": MFAAdapter().encrypt(secret)}
            )
        record_security_event("admin_mfa_provisioned", user=user)
        label = quote(f"Pyracast:{user.email or user.username}", safe="")
        parameters = urlencode({"secret": secret, "issuer": "Pyracast", "period": 30, "digits": 6})
        self.stdout.write(
            "Keep this enrollment URI private and transfer it to the administrator securely:"
        )
        self.stdout.write(f"otpauth://totp/{label}?{parameters}")
