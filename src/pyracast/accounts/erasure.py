from collections import Counter
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from functools import reduce
from operator import or_
from typing import Any

from allauth.account.models import EmailAddress
from allauth.mfa.models import Authenticator
from allauth.socialaccount.models import SocialAccount
from allauth.usersessions.models import UserSession
from django.contrib.auth.models import User
from django.contrib.sessions.models import Session
from django.db import models, transaction
from django.db.models import F, Q
from django.db.models.functions import Cast, Lower

from pyracast.accounts.audit import SecurityEvent, record_security_event
from pyracast.accounts.models import (
    AuthenticationCacheEntry,
    Membership,
    OutboundMail,
    Tenant,
    UserLanguagePreference,
    WorkspaceInvitation,
    WorkspaceRole,
)
from pyracast.accounts.processes import lock_account
from pyracast.notifications.models import DeliveryRecipient, ReminderRecipient, ReminderSchedule
from pyracast.notifications.recipients import recipient_key
from pyracast.regions.models import Region


class AdministersWorkspaceError(Exception):
    def __init__(self, account_id: int, tenant_ids: Sequence[int]) -> None:
        self.tenant_ids = tuple(tenant_ids)
        noun = "workspace" if len(self.tenant_ids) == 1 else "workspaces"
        super().__init__(
            f"Account {account_id} administers {noun} {', '.join(map(str, self.tenant_ids))}. "
            "Assign another administrator or purge each workspace first."
        )


@dataclass(frozen=True)
class Erasure:
    account_id: int
    # Rows deleted or changed, by model label.
    changes: dict[str, int]


def _locked(rows: models.QuerySet[Any]) -> None:
    list(rows.select_for_update(of=("self",)).order_by("pk").values_list("pk", flat=True))


def _count(changes: Counter[str], deleted: tuple[int, dict[str, int]]) -> None:
    changes.update({label: count for label, count in deleted[1].items() if count})


def _mentioning(field: str, addresses: Collection[str]) -> Q:
    return reduce(
        or_, (Q(**{f"{field}__contains": address}) for address in addresses), Q(pk__in=[])
    )


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
    _count(
        changes,
        WorkspaceInvitation.objects.alias(address=Lower("email"))
        .filter(Q(address__in=addresses) | Q(accepted_by_id=account_id))
        .delete(),
    )
    _count(
        changes,
        OutboundMail.objects.alias(
            address=Lower("recipient"), text=Lower(Cast("context", models.TextField()))
        )
        .filter(Q(address__in=addresses) | _mentioning("text", addresses))
        .delete(),
    )
    _count(
        changes,
        AuthenticationCacheEntry.objects.alias(text=Lower(Cast("value", models.TextField())))
        .filter(_mentioning("text", addresses))
        .delete(),
    )


def _forget_identity(account_id: int, changes: Counter[str]) -> None:
    sessions = UserSession.objects.filter(user_id=account_id).values("session_key")
    _count(changes, Session.objects.filter(session_key__in=sessions).delete())
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
    account.username = f"erased-{account.pk}"
    account.email = account.first_name = account.last_name = ""
    account.set_unusable_password()
    account.is_active = account.is_staff = account.is_superuser = False
    account.last_login = None
    account.save()


def erase_account(account_id: int, *, execute: bool) -> Erasure:
    """Anonymizes the account in place: its credentials, second factors, sessions, provider
    identities and addresses go, as do the invitations, mail and recipient entries that name
    those addresses; history that points at the account keeps pointing at it. Without
    `execute` the same work is rolled back, so the result reports what erasure would change."""
    changes: Counter[str] = Counter()
    with transaction.atomic():
        lock_account(account_id)
        account = User.objects.get(pk=account_id)
        addresses = {
            address.lower()
            for address in (
                account.email,
                *EmailAddress.objects.filter(user_id=account_id).values_list("email", flat=True),
            )
            if address
        }
        memberships = Membership.objects.filter(user_id=account_id)
        # Entries tied to the account's memberships hold its addresses, earlier ones included.
        keys = {recipient_key(address) for address in addresses} | set(
            ReminderRecipient.objects.filter(
                ties__membership_id__in=memberships.values("pk")
            ).values_list("key", flat=True)
        )
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
                Q(pk__in=memberships.values("tenant_id"))
                | Q(pk__in=Region.objects.filter(pk__in=regions).values("tenant_id"))
            )
        )
        if administered := list(
            memberships.filter(role=WorkspaceRole.ADMIN)
            .order_by("tenant_id")
            .values_list("tenant_id", flat=True)
        ):
            raise AdministersWorkspaceError(account_id, administered)
        _locked(Region.objects.filter(pk__in=regions))
        _forget_recipients(recipients, delivered, changes)
        _forget_mail(account_id, addresses, changes)
        _forget_identity(account_id, changes)
        _anonymize(account)
        changes["auth.User"] += 1
        record_security_event(SecurityEvent.ACCOUNT_ERASED, target_id=account_id)
        if not execute:
            transaction.set_rollback(True)
    return Erasure(account_id, dict(sorted(changes.items())))
