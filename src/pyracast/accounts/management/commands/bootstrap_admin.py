from io import StringIO
from typing import Any

import segno
from allauth.account.models import EmailAddress
from allauth.mfa.models import Authenticator
from allauth.mfa.recovery_codes.internal.auth import RecoveryCodes
from allauth.mfa.totp.internal.auth import TOTP, generate_totp_secret, validate_totp_code
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import CommandError, CommandParser
from django.db import transaction

from pyracast.accounts.adapters import AccountAdapter
from pyracast.accounts.audit import SecurityEvent, record_security_event
from pyracast.accounts.authorization import requires_admin_mfa
from pyracast.accounts.inputs import sign_in_password
from pyracast.accounts.management.operator import OperatorCommand, acquired_address
from pyracast.accounts.mfa import MFAAdapter

INACTIVE = "The account with this address is inactive. Nothing was changed."
CUSTOMER = (
    "The address belongs to a customer account. Use an address of its own for the administrator."
)
ENROLLED = "This administrator already has a second factor. Reset it with reset_mfa first."
PASSWORDS_DIFFER = "The two passwords differ. Nothing was stored."
POLICY_REFUSED = "The password does not meet the password policy. Nothing was stored."
WRONG_CODE = "The code does not match the new authenticator. Nothing was stored."
CHANGED = "The account changed while the command ran. Nothing was stored; run it again."
UNUSABLE = (
    "The administrator could not sign in with this address: it must be verified or the "
    "account's only, primary address. Nothing was changed."
)


def signs_in_with(account: User, address: str) -> bool:
    # The sign-in rule (authentication.holders) after `store`: an address identifies the account
    # that holds it verified, else the account whose only address row it is, when primary.
    # `store` adds a missing row unverified, primary only when the account has no primary row;
    # `account_using` has already refused an address that another account uses.
    rows = [
        (email.lower(), verified, primary)
        for email, verified, primary in EmailAddress.objects.filter(user=account).values_list(
            "email", "verified", "primary"
        )
    ]
    given = [row for row in rows if row[0] == address]
    if any(verified for _email, verified, _primary in given):
        return True
    if not given:
        rows.append((address, False, not any(primary for _email, _verified, primary in rows)))
    return len(rows) == 1 and rows[0][0] == address and rows[0][2]


class Command(OperatorCommand):
    help = (
        "Create the platform administrator for an address, or enroll a new authenticator for an "
        "administrator without one, with a new password."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--email", required=True)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args: Any, **options: Any) -> None:
        address = acquired_address(options["email"])
        account = self.eligible(address)
        if options["dry_run"]:
            plan = (
                f"enroll a new authenticator and password for the administrator {address}"
                if account
                else f"create the platform administrator {address} with a password and an "
                "authenticator"
            )
            self.stdout.write(f"Dry run: would {plan}. Nothing was changed.")
            return
        user = account or User(
            username=AccountAdapter().generate_unique_username([address]), email=address
        )
        password = self.new_password(user)
        secret = generate_totp_secret()
        self.show_enrollment(MFAAdapter().build_totp_url(user, secret), secret)
        if not validate_totp_code(secret, self.ask("Current code from the authenticator: ")):
            raise CommandError(WRONG_CODE)
        with transaction.atomic():
            current = self.eligible(address, lock=True)
            if (current.pk if current else None) != (account.pk if account else None):
                raise CommandError(CHANGED)
            user = current or user
            self.store(user, address, password, secret)
        record_security_event(
            SecurityEvent.ADMIN_MFA_PROVISIONED,
            user_id=user.pk,
            reason="enrolled" if account else "created",
        )
        self.stdout.write(
            f"Platform administrator {address} is ready. Username: {user.username}.\n"
            "At the first sign-in, enter the code mailed to this address, then the authenticator "
            "code; then save the recovery codes in Account settings."
        )

    def eligible(self, address: str, *, lock: bool = False) -> User | None:
        account = self.account_using(address, lock=lock)
        if account is None:
            return None
        if not account.is_active:
            raise CommandError(INACTIVE)
        if not requires_admin_mfa(account):
            raise CommandError(CUSTOMER)
        if Authenticator.objects.filter(user=account).exists():
            raise CommandError(ENROLLED)
        if not signs_in_with(account, address):
            raise CommandError(UNUSABLE)
        return account

    def new_password(self, user: User) -> str:
        password = self.ask("New password: ", secret=True)
        if self.ask("New password again: ", secret=True) != password:
            raise CommandError(PASSWORDS_DIFFER)
        try:
            sign_in_password(password)
            validate_password(password, user)
        except ValidationError as error:
            raise CommandError(" ".join([POLICY_REFUSED, *error.messages])) from None
        return password

    def show_enrollment(self, uri: str, secret: str) -> None:
        self.stdout.write(
            "Scan this private QR code with the administrator's authenticator. Keep the QR code, "
            "the setup key and the link out of logs and tickets."
        )
        qr = StringIO()
        segno.make_qr(uri).terminal(out=qr, compact=True)
        try:
            self.stdout.write(qr.getvalue())
        except UnicodeEncodeError:
            self.stdout.write("This console cannot display the QR code; use the setup key or link.")
        self.stdout.write(
            "Setup key: "
            + " ".join(secret[index : index + 4] for index in range(0, len(secret), 4))
        )
        self.stdout.write(f"Setup link: {uri}")

    @staticmethod
    def store(user: User, address: str, password: str, secret: str) -> None:
        user.is_staff = user.is_superuser = True
        user.email = user.email or address
        user.set_password(password)
        user.save()
        if not EmailAddress.objects.filter(user=user, email__iexact=address).exists():
            EmailAddress.objects.create(
                user=user,
                email=address,
                primary=not EmailAddress.objects.filter(user=user, primary=True).exists(),
                verified=False,
            )
        TOTP.activate(user, secret)
        RecoveryCodes.activate(user)
