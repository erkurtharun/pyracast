from datetime import datetime

from django.views.decorators.debug import sensitive_variables

from pyracast.accounts.models import WorkspaceInvitation
from pyracast.encryption import secret_cipher
from pyracast.notifications.models import CODED_MAIL, OutboundMail, OutboundMailKind

CODE_CONTEXT = b"pyracast-outbound-mail-code"


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
    lock_key: str | None = None,
) -> None:
    """Store the intent in the caller's transaction; the mailer sends it after the commit. An
    intent for a lock that already has one is not stored again."""
    mail = OutboundMail(
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
        lock_key=lock_key,
    )
    OutboundMail.objects.bulk_create([mail], ignore_conflicts=True)
