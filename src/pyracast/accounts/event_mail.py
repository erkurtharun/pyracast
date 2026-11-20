from collections.abc import Iterable
from datetime import datetime, timedelta

from allauth.account.models import EmailAddress
from django.conf import settings
from django.contrib.auth.models import User
from django.db.models import Exists, OuterRef, QuerySet

from pyracast.accounts.language import user_language
from pyracast.accounts.mail_intents import enqueue_mail
from pyracast.accounts.models import (
    Membership,
    OutboundMailKind,
    WorkspaceAdminChange,
    WorkspaceRole,
)

EVENT_MAIL_LIFETIME = timedelta(days=3)
REVIEW_MAIL = {
    "approved": OutboundMailKind.PLANT_APPROVED,
    "rejected": OutboundMailKind.PLANT_REJECTED,
    "suspended": OutboundMailKind.PLANT_SUSPENDED,
}


def reachable_members(tenant_id: int) -> QuerySet[Membership]:
    """Active members whose account address is verified: the only addresses event mail uses."""
    verified = EmailAddress.objects.filter(
        user=OuterRef("user"), email__iexact=OuterRef("user__email"), verified=True
    )
    return (
        Membership.objects.filter(tenant_id=tenant_id, user__is_active=True)
        .filter(Exists(verified))
        .select_related("user")
        .order_by("pk")
    )


def recipient_language(user: User) -> str:
    return user_language(user) or settings.LANGUAGE_CODE


def _enqueue(
    kind: OutboundMailKind,
    users: Iterable[User],
    subject_id: int,
    *,
    now: datetime,
    expires_at: datetime,
) -> None:
    for user in users:
        enqueue_mail(
            kind,
            user.email,
            language=recipient_language(user),
            expires_at=expires_at,
            now=now,
            context={"subject_id": str(subject_id)},
        )


def enqueue_review_mail(tenant_id: int, event_id: int, status: str, now: datetime) -> None:
    """Writes one intent per workspace administrator in the caller's transaction."""
    administrators = reachable_members(tenant_id).filter(role=WorkspaceRole.ADMIN)
    _enqueue(
        REVIEW_MAIL[status],
        (membership.user for membership in administrators),
        event_id,
        now=now,
        expires_at=now + EVENT_MAIL_LIFETIME,
    )


def enqueue_role_mail(change: WorkspaceAdminChange, now: datetime) -> None:
    """Writes the intents for the new and the former administrator in the caller's
    transaction."""
    members = {
        membership.user_id: membership.user
        for membership in reachable_members(change.tenant_id).filter(
            user_id__in=(change.administrator_id, change.previous_administrator_id)
        )
    }
    for user_id, kind in (
        (change.administrator_id, OutboundMailKind.WORKSPACE_ADMIN_ASSIGNED),
        (change.previous_administrator_id, OutboundMailKind.WORKSPACE_ADMIN_RELEASED),
    ):
        if user_id in members:
            _enqueue(
                kind,
                (members[user_id],),
                change.pk,
                now=now,
                expires_at=now + EVENT_MAIL_LIFETIME,
            )
