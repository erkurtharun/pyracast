import getpass
import sys
from typing import Any, TextIO

from allauth.account.models import EmailAddress
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.core.validators import validate_email
from django.db import connection
from django.db.models import Q, QuerySet

from pyracast.accounts.addresses import plain_address
from pyracast.accounts.processes import lock_account

INVALID_ADDRESS = "Enter a valid email address."
SHARED_ADDRESS = "The address belongs to more than one account. Resolve that first."
# Advisory lock key ("PYOP") that serializes the final step of operator commands on accounts.
OPERATOR_LOCK = 0x50594F50


def lookup_address(value: str) -> str:
    address = value.strip().lower()
    try:
        validate_email(address)
    except ValidationError:
        raise CommandError(INVALID_ADDRESS) from None
    if len(address) > 254:
        raise CommandError(INVALID_ADDRESS)
    return address


def acquired_address(value: str) -> str:
    try:
        return plain_address(value.strip().lower())
    except ValidationError:
        raise CommandError(INVALID_ADDRESS) from None


class OperatorCommand(BaseCommand):
    # Answers come from `stdin` (the terminal, a pipe or a test) and never reach a log, an audit
    # line or an exception message.
    stealth_options = ("stdin",)
    stdin: TextIO

    def execute(self, *args: Any, **options: Any) -> str | None:
        self.stdin = options.get("stdin") or sys.stdin
        return super().execute(*args, **options)

    def ask(self, prompt: str, *, secret: bool = False) -> str:
        if secret and self.stdin.isatty():
            return getpass.getpass(prompt)
        self.stdout.write(prompt, ending="")
        self.stdout.flush()
        return self.stdin.readline().rstrip("\r\n")

    def account_using(self, address: str, *, lock: bool = False) -> User | None:
        holders = User.objects.filter(
            Q(email__iexact=address)
            | Q(pk__in=EmailAddress.objects.filter(email__iexact=address).values("user_id"))
        ).order_by("pk")
        if lock:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_xact_lock(%s)", [OPERATOR_LOCK])
        account = self._only(holders)
        if lock and account is not None:
            # The account's rows are locked in the account lock order and the holder is read
            # again under the locks; callers compare it with the account they started with.
            lock_account(account.pk)
            account = self._only(holders.all())
        return account

    @staticmethod
    def _only(holders: QuerySet[User]) -> User | None:
        accounts = list(holders)
        if len(accounts) > 1:
            raise CommandError(SHARED_ADDRESS)
        return accounts[0] if accounts else None
