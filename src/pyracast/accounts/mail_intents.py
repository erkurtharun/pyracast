from datetime import datetime, timedelta

from django.db import connections, transaction
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

from pyracast.accounts.models import CODED_MAIL, OutboundMail, OutboundMailKind, WorkspaceInvitation
from pyracast.encryption import secret_cipher

CODE_CONTEXT = b"pyracast-outbound-mail-code"
# The failure history of a sign-in lives in the authentication cache; its notice is written
# through the same autocommitted connection.
LOCK_NOTICE_DATABASE = "auth_cache"
LOCK_NOTICE_INTERVAL = timedelta(hours=24)
# Two-key advisory lock space ("PYLN") that serializes the lock notices of one address.
LOCK_NOTICE_LOCK_SPACE = 0x50594C4E


@sensitive_variables("code")
def seal_code(code: str) -> str:
    return secret_cipher(CODE_CONTEXT, primary_only=True).encrypt(code.encode()).decode("ascii")


@sensitive_variables("code")
def enqueue_mail(
    kind: OutboundMailKind,
    recipient: str,
    *,
    language: str,
    expires_at: datetime,
    owner_language: bool = False,
    now: datetime,
    invitation: WorkspaceInvitation | None = None,
    context: dict[str, str] | None = None,
    code: str = "",
) -> None:
    """Store the intent in the caller's transaction; the mailer sends it after the commit."""
    OutboundMail.objects.create(
        kind=kind,
        recipient=recipient,
        invitation=invitation,
        language=language,
        owner_language=owner_language,
        context=context or {},
        sealed_code=seal_code(code) if kind in CODED_MAIL else "",
        created_at=now,
        available_at=now,
        expires_at=expires_at,
    )


def enqueue_lock_notice(address: str, *, language: str, lifetime: timedelta) -> None:
    """Make sure the attempted address has a daily-lock notice from the last 24 hours: one is
    stored unless it exists, so a lock always has one and no address gets more.

    The notice commits on its own, so no rollback of the sign-in separates it from the lock.
    """
    with transaction.atomic(using=LOCK_NOTICE_DATABASE, durable=True):
        with connections[LOCK_NOTICE_DATABASE].cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(%s, hashtext(%s))", [LOCK_NOTICE_LOCK_SPACE, address]
            )
        now = timezone.now()
        notices = OutboundMail.objects.using(LOCK_NOTICE_DATABASE)
        if notices.filter(
            kind=OutboundMailKind.LOGIN_LOCKED,
            recipient=address,
            created_at__gt=now - LOCK_NOTICE_INTERVAL,
        ).exists():
            return
        notices.create(
            kind=OutboundMailKind.LOGIN_LOCKED,
            recipient=address,
            language=language,
            owner_language=True,
            created_at=now,
            available_at=now,
            expires_at=now + lifetime,
        )
