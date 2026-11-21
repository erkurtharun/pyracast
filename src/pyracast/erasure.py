import json
from collections import Counter
from collections.abc import Collection, Iterator, Sequence
from dataclasses import dataclass
from functools import reduce
from operator import or_
from typing import Any

from allauth.account.models import EmailAddress
from allauth.mfa.models import Authenticator
from allauth.socialaccount.models import SocialAccount
from allauth.usersessions.models import UserSession
from django.contrib.auth import SESSION_KEY
from django.contrib.auth.models import User
from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.models import Session
from django.db import OperationalError, connection, models, transaction
from django.db.models import F, Q
from django.db.models.expressions import RawSQL
from django.db.models.fields.json import KT
from django.db.models.functions import Lower
from django.utils import timezone
from psycopg.errors import DeadlockDetected, LockNotAvailable

from pyracast.accounts.audit import SecurityEvent, record_security_event
from pyracast.accounts.authorization import is_platform_admin
from pyracast.accounts.management.operator import OPERATOR_LOCK
from pyracast.accounts.models import (
    MAIL_ADDRESS_KEYS,
    AuthenticationCacheEntry,
    Membership,
    OutboundMail,
    OutboundMailKind,
    Tenant,
    UserLanguagePreference,
    WorkspaceInvitation,
    WorkspaceRole,
)
from pyracast.accounts.processes import PROCESSES, lock_account
from pyracast.notifications.models import DeliveryRecipient, ReminderRecipient, ReminderSchedule
from pyracast.notifications.recipients import recipient_key
from pyracast.regions.models import Region

# Mail that names an address the account changed away from or removed, beside its current one.
ADDRESS_HISTORY_MAIL = (OutboundMailKind.EMAIL_CHANGED, OutboundMailKind.EMAIL_DELETED)
# Rows of unexpired sessions read per round trip while looking for pending flows.
SESSION_CHUNK = 500


class ErasureError(Exception):
    pass


class AdministersWorkspaceError(ErasureError):
    def __init__(self, account_id: int, tenant_ids: Sequence[int]) -> None:
        self.tenant_ids = tuple(tenant_ids)
        noun = "workspace" if len(self.tenant_ids) == 1 else "workspaces"
        super().__init__(
            f"Account {account_id} administers {noun} {', '.join(map(str, self.tenant_ids))}. "
            "Assign another administrator or purge each workspace first."
        )


class LastPlatformAdministratorError(ErasureError):
    def __init__(self, account_id: int) -> None:
        super().__init__(
            f"Account {account_id} is the last active platform administrator. Create another "
            "one with bootstrap_admin first."
        )


class ErasedNameTakenError(ErasureError):
    def __init__(self, account_id: int, holder_id: int) -> None:
        super().__init__(
            f"Account {holder_id} holds the username erasure gives account {account_id}. "
            f"Rename account {holder_id} first."
        )


class ErasureInterruptedError(ErasureError):
    def __init__(self, account_id: int) -> None:
        super().__init__(
            f"Erasure of account {account_id} waited for a lock held by concurrent work and "
            "changed nothing. Run it again."
        )


@dataclass(frozen=True)
class Erasure:
    account_id: int
    # Rows deleted or changed, by model label.
    changes: dict[str, int]
    already_erased: bool = False
    platform_administrator: bool = False
    # Addresses of the account that another account holds now; their rows were kept.
    addresses_left_out: int = 0
    staff: bool = False


def anonymized(account_id: int) -> dict[str, Any]:
    """The account fields erasure leaves; with an unusable password they mark it as erased."""
    return {
        "username": f"erased-{account_id}",
        "email": "",
        "first_name": "",
        "last_name": "",
        "is_active": False,
        "is_staff": False,
        "is_superuser": False,
        "last_login": None,
    }


def is_erased(account: User) -> bool:
    return not account.has_usable_password() and all(
        getattr(account, field) == value for field, value in anonymized(account.pk).items()
    )


def _locked(rows: models.QuerySet[Any]) -> None:
    list(rows.select_for_update(of=("self",)).order_by("pk").values_list("pk", flat=True))


def _count(changes: Counter[str], deleted: tuple[int, dict[str, int]]) -> None:
    changes.update({label: count for label, count in deleted[1].items() if count})


def _naming(addresses: Collection[str]) -> Q:
    # Exact lower-cased values of the recipient and of the context keys that hold an address.
    return reduce(
        or_,
        (Q(**{f"named_{key}__in": addresses}) for key in MAIL_ADDRESS_KEYS),
        Q(named_recipient__in=addresses),
    )


def _mail() -> models.QuerySet[OutboundMail]:
    return OutboundMail.objects.alias(
        named_recipient=Lower("recipient"),
        **{f"named_{key}": Lower(KT(f"context__{key}")) for key in MAIL_ADDRESS_KEYS},
    )


def _normalized(values: Collection[object]) -> set[str]:
    return {value.strip().lower() for value in values if isinstance(value, str) and value.strip()}


def _held_elsewhere(account_id: int, addresses: Collection[str]) -> set[str]:
    return {
        address
        for rows in (
            User.objects.exclude(pk=account_id),
            EmailAddress.objects.exclude(user_id=account_id),
        )
        for address in rows.annotate(address=Lower("email"))
        .filter(address__in=addresses)
        .values_list("address", flat=True)
    }


def _addresses(account: User) -> tuple[set[str], int]:
    """The account's current addresses, the earlier ones its reminder entries still tie to its
    memberships, and every address the outbox names as changed or removed for one of them, with
    how many were left out. An address another account holds now is left out and not followed:
    its rows stay and age out by the retention periods. Each round adds at least one address the
    outbox names, so the rounds are bounded by those."""
    found: set[str] = set()
    held: set[str] = set()
    new = _normalized(
        [account.email, *EmailAddress.objects.filter(user=account).values_list("email", flat=True)]
    ) | _normalized(
        ReminderRecipient.objects.filter(
            ties__membership_id__in=Membership.objects.filter(user=account).values("pk")
        ).values_list("address", flat=True)
    )
    while new:
        held |= _held_elsewhere(account.pk, new)
        new -= held
        found |= new
        named: set[str] = set()
        for recipient, context in (
            _mail()
            .filter(_naming(new), kind__in=ADDRESS_HISTORY_MAIL)
            .values_list("recipient", "context")
        ):
            named |= _normalized([recipient, *(context.get(key) for key in MAIL_ADDRESS_KEYS)])
        new = named - found - held
    return found, len(held)


def _refuse_administrator(account_id: int) -> None:
    if administered := list(
        Membership.objects.filter(user_id=account_id, role=WorkspaceRole.ADMIN)
        .order_by("tenant_id")
        .values_list("tenant_id", flat=True)
    ):
        raise AdministersWorkspaceError(account_id, administered)


def _forget_recipients(
    recipients: models.QuerySet[ReminderRecipient],
    delivered: models.QuerySet[DeliveryRecipient],
    changes: Counter[str],
) -> None:
    schedules = set(recipients.values_list("schedule_id", flat=True))
    _count(changes, recipients.delete())
    changes["notifications.ReminderSchedule"] += ReminderSchedule.objects.filter(
        pk__in=schedules
    ).update(revision=F("revision") + 1)
    _count(changes, delivered.delete())


def _forget_mail(account_id: int, addresses: Collection[str], changes: Counter[str]) -> None:
    invitations = WorkspaceInvitation.objects.alias(address=Lower("email")).filter(
        Q(address__in=addresses) | Q(accepted_by_id=account_id)
    )
    # Mail before its invitation, each in id order: the order of the mail worker.
    mail = _mail().filter(_naming(addresses) | Q(invitation__in=invitations))
    _locked(mail)
    _count(changes, mail.delete())
    _locked(invitations)
    _count(changes, invitations.delete())
    holding = RawSQL(
        "jsonb_path_exists(lower(value::text)::jsonb, 'lax $.** ? (@ == $addresses)', "
        "jsonb_build_object('addresses', %s::jsonb))",
        [json.dumps(sorted(addresses))],
        output_field=models.BooleanField(),
    )
    _count(changes, AuthenticationCacheEntry.objects.filter(holding).delete())


def _strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value.strip().casefold()
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _holds_flow_of(data: dict[str, Any], account: User, addresses: Collection[str]) -> bool:
    if data.get(SESSION_KEY) == str(account.pk):
        return True
    if any(
        isinstance(value := data.get(process.session_key), dict)
        and process.account(value) == account.pk
        for process in PROCESSES
    ):
        return True
    strings = set(_strings(data))
    names = {account.first_name.strip().casefold(), account.last_name.strip().casefold()}
    return bool(strings & {address.casefold() for address in addresses}) or (
        "" not in names and names <= strings
    )


def _forget_sessions(account: User, addresses: Collection[str], changes: Counter[str]) -> None:
    keys = set(UserSession.objects.filter(user=account).values_list("session_key", flat=True))
    # A session without a device row has no signed-in account but may hold a pending flow of the
    # person (a reset, a verification, a provider sign-up): every unexpired one is read once.
    store = SessionStore()
    unbound = (
        Session.objects.filter(expire_date__gt=timezone.now())
        .exclude(session_key__in=UserSession.objects.values("session_key"))
        .values_list("session_key", "session_data")
    )
    keys |= {
        key
        for key, data in unbound.iterator(chunk_size=SESSION_CHUNK)
        if _holds_flow_of(store.decode(data), account, addresses)
    }
    _count(changes, Session.objects.filter(session_key__in=keys).delete())


def _forget_identity(account_id: int, changes: Counter[str]) -> None:
    for rows in (
        UserSession,
        EmailAddress,
        SocialAccount,
        Authenticator,
        Membership,
        UserLanguagePreference,
        User.groups.through,
        User.user_permissions.through,
    ):
        _count(changes, rows._default_manager.filter(user_id=account_id).delete())


def _anonymize(account: User) -> None:
    for field, value in anonymized(account.pk).items():
        setattr(account, field, value)
    account.set_unusable_password()
    account.save()


def _erase(account_id: int) -> Erasure:
    # The operator lock serializes this with the commands that grant the platform role.
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [OPERATOR_LOCK])
    lock_account(account_id)
    account = User.objects.get(pk=account_id)
    if is_erased(account):
        return Erasure(account_id, {}, already_erased=True)
    username = anonymized(account_id)["username"]
    if holder := User.objects.filter(username=username).exclude(pk=account_id).first():
        raise ErasedNameTakenError(account_id, holder.pk)
    administrator = is_platform_admin(account)
    if (
        administrator
        and not User.objects.filter(is_active=True, is_staff=True, is_superuser=True)
        .exclude(pk=account_id)
        .exists()
    ):
        raise LastPlatformAdministratorError(account_id)
    addresses, left_out = _addresses(account)
    keys = {recipient_key(address) for address in addresses}
    recipients = ReminderRecipient.objects.filter(key__in=keys)
    delivered = DeliveryRecipient.objects.filter(key__in=keys)
    regions = Region.objects.filter(
        Q(reminderschedule__recipient_rows__in=recipients)
        | Q(reminderschedule__notificationdelivery__recipient_rows__in=delivered)
    ).values("pk")
    # Workspaces before their regions, the order of administrator changes, member removals
    # and reminder writers: an administrator assignment that commits first is seen below.
    _locked(
        Tenant.objects.filter(
            Q(pk__in=Membership.objects.filter(user_id=account_id).values("tenant_id"))
            | Q(pk__in=Region.objects.filter(pk__in=regions).values("tenant_id"))
        )
    )
    _refuse_administrator(account_id)
    _locked(Region.objects.filter(pk__in=regions))
    changes: Counter[str] = Counter()
    _forget_recipients(recipients, delivered, changes)
    _forget_mail(account_id, addresses, changes)
    _forget_sessions(account, addresses, changes)
    _forget_identity(account_id, changes)
    staff = account.is_staff or account.is_superuser
    _anonymize(account)
    changes["auth.User"] += 1
    record_security_event(SecurityEvent.ACCOUNT_ERASED, target_id=account_id)
    return Erasure(
        account_id,
        dict(sorted(changes.items())),
        platform_administrator=administrator,
        staff=staff,
        addresses_left_out=left_out,
    )


def erase_account(account_id: int, *, execute: bool) -> Erasure:
    """Anonymizes the account in place: its credentials, second factors, sessions, provider
    identities and addresses go, as do the invitations, mail, cache entries, pending flows and
    recipient entries that name those addresses, except an address another account holds now;
    history that points at the account keeps pointing at it. Without `execute` the same work is
    rolled back, so the result reports what erasure would change."""
    if is_erased(User.objects.get(pk=account_id)):
        return Erasure(account_id, {}, already_erased=True)
    # Refused before any lock: workspace writers lock the workspace before its administrator.
    _refuse_administrator(account_id)
    try:
        with transaction.atomic():
            erasure = _erase(account_id)
            if not execute:
                transaction.set_rollback(True)
    except OperationalError as error:
        if isinstance(error.__cause__, DeadlockDetected | LockNotAvailable):
            raise ErasureInterruptedError(account_id) from None
        raise
    return erasure
