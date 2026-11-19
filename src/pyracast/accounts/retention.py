import logging
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any

from allauth.account.models import EmailAddress
from allauth.socialaccount.models import SocialAccount
from allauth.usersessions.models import UserSession
from django.contrib.auth.models import User
from django.contrib.sessions.models import Session
from django.db import transaction
from django.db.models import (
    Exists,
    Model,
    OuterRef,
    ProtectedError,
    Q,
    QuerySet,
    RestrictedError,
)

from pyracast.accounts.models import Membership, Tenant, WorkspaceInvitation, WorkspaceRole
from pyracast.background.failures import isolate

logger = logging.getLogger(__name__)
BATCH_SIZE = 500
ABANDONED_AFTER = timedelta(days=7)


def end_expired_sessions(now: datetime) -> int:
    expired = list(
        Session.objects.filter(expire_date__lt=now)
        .order_by("expire_date")
        .values_list("session_key", flat=True)[:BATCH_SIZE]
    )
    removed = Session.objects.filter(session_key__in=expired).delete()[0]
    orphans = list(
        UserSession.objects.exclude(session_key__in=Session.objects.values("session_key"))
        .order_by("pk")
        .values_list("pk", flat=True)[:BATCH_SIZE]
    )
    return removed + UserSession.objects.filter(pk__in=orphans).delete()[0]


def _abandoned(cutoff: datetime) -> QuerySet[User]:
    # Never signed in, no proven address, and every workspace it belongs to is its own and empty.
    shared = Membership.objects.filter(tenant_id=OuterRef("tenant_id")).exclude(
        user_id=OuterRef("user_id")
    )
    invited = WorkspaceInvitation.objects.filter(tenant_id=OuterRef("tenant_id"))
    kept = Membership.objects.filter(user_id=OuterRef("pk")).filter(
        ~Q(role=WorkspaceRole.ADMIN) | Q(Exists(shared)) | Q(Exists(invited))
    )
    return (
        User.objects.filter(
            last_login__isnull=True, date_joined__lte=cutoff, is_staff=False, is_superuser=False
        )
        .exclude(emailaddress__verified=True)
        .exclude(Exists(kept))
    )


def _lock[M: Model](rows: QuerySet[M]) -> list[Any]:
    return list(rows.select_for_update(of=("self",)).order_by("pk").values_list("pk", flat=True))


def reclaim_signup(user_id: int, cutoff: datetime) -> bool:
    abandoned = _abandoned(cutoff).filter(pk=user_id)
    with transaction.atomic():
        # One order with the requests that start or complete account processes (addresses,
        # provider identities, account) and workspace operations (the workspace before its
        # invitations and members); a new statement after these locks sees what committed while
        # it waited. An account that signed in meanwhile is kept before its workspaces are locked:
        # its own workspace operations hold the workspace while they reference the account row.
        _lock(EmailAddress.objects.filter(user_id=user_id))
        _lock(SocialAccount.objects.filter(user_id=user_id))
        _lock(User.objects.filter(pk=user_id))
        if not abandoned.exists():
            return False
        tenants = _lock(Tenant.objects.filter(membership__user_id=user_id))
        if not abandoned.exists():
            return False
        try:
            with transaction.atomic():
                Tenant.objects.filter(pk__in=tenants).delete()
                User.objects.filter(pk=user_id).delete()
        except ProtectedError, RestrictedError:
            return False
    return True


def sweep_accounts(now: datetime) -> None:
    end_expired_sessions(now)
    reclaim_abandoned_signups(now)


def reclaim_abandoned_signups(now: datetime) -> int:
    cutoff = now - ABANDONED_AFTER
    candidates = _abandoned(cutoff).order_by("pk").values_list("pk", flat=True)
    reclaimed = 0

    def scan() -> Iterator[int]:
        # The scan moves past kept and failing candidates, which stay eligible for the next run,
        # so they never hold back later ones.
        after = 0
        while reclaimed < BATCH_SIZE and (
            page := list(candidates.filter(pk__gt=after)[: BATCH_SIZE - reclaimed])
        ):
            yield from page
            after = page[-1]

    def reclaim(user_id: int) -> None:
        nonlocal reclaimed
        reclaimed += reclaim_signup(user_id, cutoff)

    try:
        isolate(
            scan(),
            reclaim,
            logger=logger,
            event="signup_reclaim_failed",
            name=lambda user_id: f"account:{user_id}",
            back_off=lambda _user_id: None,
        )
    finally:
        if reclaimed:
            logger.info("abandoned_signups_reclaimed count=%s", reclaimed)
    return reclaimed
