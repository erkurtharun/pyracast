from typing import Any

from allauth.account.models import EmailAddress
from allauth.mfa.models import Authenticator
from allauth.usersessions.models import UserSession
from django.contrib.auth.models import User
from django.core.management.base import CommandError, CommandParser
from django.db import transaction

from pyracast.accounts.adapters import AccountAdapter
from pyracast.accounts.audit import SecurityEvent, record_security_event
from pyracast.accounts.authorization import requires_admin_mfa
from pyracast.accounts.management.operator import OperatorCommand, operator_address
from pyracast.accounts.sessions import end_sessions

UNKNOWN = "No account uses this address."
NO_FACTOR = "This account has no second factor. Nothing was changed."
MISMATCH = "The confirmation does not match the address. Nothing was changed."
NOTICE = "account/email/second_factor_reset"


def counted(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


class Command(OperatorCommand):
    help = (
        "Remove every second factor of the account with an address and end all its sessions, "
        "for a lost or compromised authenticator."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--email", required=True)
        parser.add_argument("--reason", required=True, choices=("lost_device", "compromised"))
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args: Any, **options: Any) -> None:
        address = operator_address(options["email"])
        account = self.with_factors(address)
        if options["dry_run"]:
            factors = Authenticator.objects.filter(user=account).count()
            sessions = UserSession.objects.filter(user=account).count()
            self.stdout.write(
                f"Dry run: would remove {counted(factors, 'second factor')} and end "
                f"{counted(sessions, 'session')} of {address}. Nothing was changed."
            )
            return
        if self.ask("Type the address again to confirm: ").strip().lower() != address:
            raise CommandError(MISMATCH)
        with transaction.atomic():
            account = self.with_factors(address, lock=True)
            factors, _removed = Authenticator.objects.filter(user=account).delete()
            sessions = end_sessions(account.pk, "mfa_reset")
            recipient = EmailAddress.objects.get_primary_email(account) or address
            AccountAdapter().send_mail(NOTICE, recipient, {"user": account})
        record_security_event(
            SecurityEvent.MFA_RESET_BY_OPERATOR,
            user_id=account.pk,
            reason=options["reason"],
            count=factors,
        )
        next_step = (
            "Enroll a new authenticator with bootstrap_admin."
            if requires_admin_mfa(account)
            else "The account can set up an authenticator again in Account settings."
        )
        self.stdout.write(
            f"Removed {counted(factors, 'second factor')} and ended "
            f"{counted(sessions, 'session')} of {address}. {next_step}"
        )

    def with_factors(self, address: str, *, lock: bool = False) -> User:
        account = self.account_using(address, lock=lock)
        if account is None:
            raise CommandError(UNKNOWN)
        if not Authenticator.objects.filter(user=account).exists():
            raise CommandError(NO_FACTOR)
        return account
