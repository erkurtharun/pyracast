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
from django.db.models import Exists, F, OuterRef, Q
from django.db.models.expressions import RawSQL
from django.db.models.fields.json import KT
from django.db.models.functions import Collate, Lower
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
from pyracast.accounts.processes import PROCESSES, PROVIDER_SIGNUP_SESSION_KEY, lock_account
from pyracast.notifications.models import (
    DeliveryRecipient,
    ReminderRecipient,
    ReminderRecipientTie,
    ReminderSchedule,
)
from pyracast.notifications.recipients import recipient_key
from pyracast.regions.models import Region

# Mail that names an address the account changed away from or removed, beside its current one.
ADDRESS_HISTORY_MAIL = (OutboundMailKind.EMAIL_CHANGED, OutboundMailKind.EMAIL_DELETED)
# PostgreSQL's built-in full Unicode case mapping. In every database locale it lower-cases a
# letter as Python's `str.lower` does, for every letter of Unicode 16; the authentication cache
# match below names it in its SQL.
CASE_MAPPING = "pg_unicode_fast"
MAIL_NAMES = ("named_recipient", *(f"named_{key}" for key in MAIL_ADDRESS_KEYS))
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
    # Addresses of the account that another account held verified when erasure began.
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


def _locked(rows: models.QuerySet[Any], field: str = "pk") -> dict[int, Any]:
    """Locks the rows in id order; maps the id of each locked row to its `field`."""
    return dict(rows.select_for_update(of=("self",)).order_by("pk").values_list("pk", field))


def _count(changes: Counter[str], deleted: tuple[int, dict[str, int]]) -> None:
    changes.update({label: count for label, count in deleted[1].items() if count})


def _folded(expression: Any) -> Lower:
    return Lower(Collate(expression, CASE_MAPPING))


def _normalized(values: Collection[object]) -> set[str]:
    # The same rule as `_folded`, on the Python side.
    return {value.strip().lower() for value in values if isinstance(value, str) and value.strip()}


def _holders(account_id: int) -> models.QuerySet[EmailAddress]:
    """Verified address rows of the other accounts. Only such a row makes an address an
    account's: a password sign-up, a provider sign-in without a verified provider address and
    `bootstrap_admin` store the row unverified until a code mailed to it is entered, and an
    account without a row for its `User.email` cannot sign in with it."""
    return EmailAddress.objects.filter(verified=True).exclude(user_id=account_id)


def _unheld(account_id: int, folded: str) -> Q:
    # Evaluated by the statement that locks or deletes the row: an address another account
    # verified while erasure waited for a lock counts as held there.
    held = _holders(account_id).annotate(address=_folded("email")).filter(address=OuterRef(folded))
    return Q(~Exists(held))


def _held_elsewhere(account_id: int, addresses: Collection[str]) -> set[str]:
    return set(
        _holders(account_id)
        .annotate(address=_folded("email"))
        .filter(address__in=addresses)
        .values_list("address", flat=True)
    )


def _naming(account_id: int, addresses: Collection[str]) -> Q:
    # The recipient or a context key that holds an address equals one of the addresses, and no
    # other account holds that address verified.
    return reduce(
        or_, (Q(**{f"{name}__in": addresses}) & _unheld(account_id, name) for name in MAIL_NAMES)
    )


def _mail() -> models.QuerySet[OutboundMail]:
    return OutboundMail.objects.alias(
        named_recipient=_folded("recipient"),
        **{f"named_{key}": _folded(KT(f"context__{key}")) for key in MAIL_ADDRESS_KEYS},
    )


def _memberships(account_id: int) -> models.QuerySet[Membership, Any]:
    return Membership.objects.filter(user_id=account_id).values("pk")


def _ties(account_id: int) -> models.QuerySet[ReminderRecipientTie]:
    return ReminderRecipientTie.objects.filter(membership_id__in=_memberships(account_id))


def _entries(account_id: int, addresses: Collection[str]) -> models.QuerySet[ReminderRecipient]:
    """Reminder entries of the addresses no other account holds verified, and the entries tied
    only to the account's memberships, whatever address they hold."""
    ties = ReminderRecipientTie.objects.filter(recipient=OuterRef("pk"))
    ours = _memberships(account_id)
    return ReminderRecipient.objects.alias(folded=_folded("address")).filter(
        Q(key__in={recipient_key(address) for address in addresses}) & _unheld(account_id, "folded")
        | Q(Exists(ties.filter(membership_id__in=ours)))
        & ~Exists(ties.exclude(membership_id__in=ours))
    )


def _delivered(account_id: int, addresses: Collection[str]) -> models.QuerySet[DeliveryRecipient]:
    return DeliveryRecipient.objects.alias(folded=_folded("address")).filter(
        Q(key__in={recipient_key(address) for address in addresses}) & _unheld(account_id, "folded")
    )


def _addresses(account: User) -> tuple[set[str], int]:
    """The account's current addresses, the earlier ones its reminder entries still tie to its
    memberships, and every address the outbox names as changed or removed for one of them, with
    how many were left out. An address another account holds verified is left out and not
    followed. Of its rows, mail, invitations, sessions and authentication cache entries age out
    by their retention periods or expiry; reminder entries other memberships or administrators
    still hold and delivery recipient rows have no period. Each round adds at least one address
    the outbox names, so the rounds are bounded by those."""
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
            .filter(_naming(account.pk, new), kind__in=ADDRESS_HISTORY_MAIL)
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
    account_id: int, addresses: Collection[str], regions: Collection[int], changes: Counter[str]
) -> None:
    # Only rows of the locked regions, each deleted by the id it was locked under: a row another
    # client commits later is left, and no schedule changes without its revision.
    entries = _locked(
        _entries(account_id, addresses).filter(schedule__region__in=regions), "schedule_id"
    )
    ties = _locked(
        _ties(account_id)
        .filter(recipient__schedule__region__in=regions)
        .exclude(recipient__in=list(entries)),
        "recipient__schedule_id",
    )
    _count(changes, ReminderRecipient.objects.filter(pk__in=entries).delete())
    _count(changes, ReminderRecipientTie.objects.filter(pk__in=ties).delete())
    changes["notifications.ReminderSchedule"] += ReminderSchedule.objects.filter(
        pk__in={*entries.values(), *ties.values()}
    ).update(revision=F("revision") + 1)
    delivered = _locked(
        _delivered(account_id, addresses).filter(delivery__schedule__region__in=regions)
    )
    _count(changes, DeliveryRecipient.objects.filter(pk__in=delivered).delete())


def _forget_mail(account_id: int, addresses: Collection[str], changes: Counter[str]) -> None:
    invitations = WorkspaceInvitation.objects.alias(folded=_folded("email")).filter(
        Q(folded__in=addresses) & _unheld(account_id, "folded") | Q(accepted_by_id=account_id)
    )
    # Mail before its invitation, each in id order: the order of the mail worker. Each delete
    # applies the guards again: a lock statement that waited for a row still decides on the
    # addresses held when it began.
    named = _naming(account_id, addresses) | Q(invitation__in=invitations)
    mail = _locked(_mail().filter(named))
    _count(changes, _mail().filter(named, pk__in=mail).delete())
    # An invitation whose mail was queued after that lock stays: deleting it would delete the
    # mail out of that order.
    unmailed = invitations.exclude(Exists(OutboundMail.objects.filter(invitation=OuterRef("pk"))))
    _count(changes, invitations.filter(pk__in=_locked(unmailed)).delete())
    # The addresses no other account holds verified when this statement runs, as one JSON array.
    holding = RawSQL(
        "jsonb_path_exists(lower(value::text COLLATE pg_unicode_fast)::jsonb, "
        "'lax $.** ? (@ == $addresses)', jsonb_build_object('addresses', "
        "(SELECT coalesce(jsonb_agg(address), '[]') FROM unnest(%s::text[]) AS address "
        "WHERE NOT EXISTS (SELECT FROM account_emailaddress AS held WHERE held.verified "
        "AND held.user_id <> %s AND lower(held.email COLLATE pg_unicode_fast) = address))))",
        [sorted(addresses), account_id],
        output_field=models.BooleanField(),
    )
    _count(changes, AuthenticationCacheEntry.objects.filter(holding).delete())


def _strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value.strip().lower()
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _name(value: object) -> str:
    return value.strip().casefold() if isinstance(value, str) else ""


def _signs_up_as(data: dict[str, Any], account: User) -> bool:
    pending = data.get(PROVIDER_SIGNUP_SESSION_KEY)
    user = pending.get("user") if isinstance(pending, dict) else None
    names = (_name(account.first_name), _name(account.last_name))
    return (
        all(names)
        and isinstance(user, dict)
        and (_name(user.get("first_name")), _name(user.get("last_name"))) == names
    )


def _holds_flow_of(data: dict[str, Any], account: User, addresses: Collection[str]) -> bool:
    """Signed in as the account or holding a pending process of it, holding one of its
    addresses, or a pending provider sign-up under its first and last name that names no other
    account."""
    owners = {
        str(owner)
        for process in PROCESSES
        if isinstance(value := data.get(process.session_key), dict)
        and (owner := process.account(value)) is not None
    }
    if (signed_in := data.get(SESSION_KEY)) is not None:
        owners.add(str(signed_in))
    if str(account.pk) in owners or not set(addresses).isdisjoint(_strings(data)):
        return True
    return not owners and _signs_up_as(data, account)


def _forget_sessions(account: User, addresses: Collection[str], changes: Counter[str]) -> None:
    keys = set(UserSession.objects.filter(user=account).values_list("session_key", flat=True))
    # Sessions are matched outside SQL, so the held addresses are read again just before.
    addresses = set(addresses) - _held_elsewhere(account.pk, addresses)
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
    regions = Region.objects.filter(
        Q(reminderschedule__recipient_rows__in=_entries(account_id, addresses))
        | Q(reminderschedule__recipient_rows__ties__in=_ties(account_id))
        | Q(
            reminderschedule__notificationdelivery__recipient_rows__in=_delivered(
                account_id, addresses
            )
        )
    ).values("pk")
    # Workspaces before their regions, the order of administrator changes, member removals
    # and reminder writers: an administrator assignment that commits first is seen below. A
    # region is locked only with its workspace.
    workspaces = _locked(
        Tenant.objects.filter(
            Q(pk__in=Membership.objects.filter(user_id=account_id).values("tenant_id"))
            | Q(pk__in=Region.objects.filter(pk__in=regions).values("tenant_id"))
        )
    )
    _refuse_administrator(account_id)
    locked_regions = _locked(Region.objects.filter(pk__in=regions, tenant_id__in=list(workspaces)))
    changes: Counter[str] = Counter()
    _forget_recipients(account_id, addresses, list(locked_regions), changes)
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
    recipient entries that name those addresses, except an address another account holds
    verified, and the ties of reminder entries to its memberships; history that points at the
    account keeps pointing at it. Without `execute` the same work is
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
