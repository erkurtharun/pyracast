from datetime import datetime
from typing import Self

from django.conf import settings
from django.db import models

from pyracast.db import choices_check, json_shape_check, lease_check
from pyracast.mail.errors import MailFailure

# Template values an outbound mail may keep: strings only, never a code, key or link.
MAIL_CONTEXT_KEYS = (
    "from_email",
    "to_email",
    "deleted_email",
    "provider",
    "timestamp",
    "ip",
    "user_agent",
    "subject_id",
)
CONTEXT_ITEMS = (
    'strict $.keyvalue() ? (@.value.type() != "string"'
    f' || !(@.key like_regex "^({"|".join(MAIL_CONTEXT_KEYS)})$"))'
)


class Language(models.TextChoices):
    EN = "en", "English"
    TR = "tr", "Türkçe"


class WorkspaceRole(models.TextChoices):
    ADMIN = "admin", "Administrator"
    VIEWER = "viewer", "Viewer"


class InvitationDeliveryStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    SENT = "sent", "Sent"
    FAILED = "failed", "Failed"
    UNCERTAIN = "uncertain", "Uncertain"


class InvitationState(models.TextChoices):
    PENDING = "pending", "Pending"
    ACCEPTED = "accepted", "Accepted"
    REVOKED = "revoked", "Revoked"
    EXPIRED = "expired", "Expired"
    SUPERSEDED = "superseded", "Superseded"


class InvitationRevocation(models.TextChoices):
    CANCELLED = "cancelled", "Cancelled"
    SUPERSEDED = "superseded", "Superseded"
    MEMBER_REMOVED = "member_removed", "Member removed"
    ADMINISTRATOR_CHANGED = "administrator_changed", "Administrator changed"


class Tenant(models.Model):
    name = models.CharField(max_length=200)

    class Meta:
        constraints = (
            models.CheckConstraint(
                condition=~models.Q(name__regex=r"^\s*$"), name="%(class)s_name_chk"
            ),
        )

    def __str__(self) -> str:
        return self.name


class Membership(models.Model):
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    role = models.CharField(max_length=6, choices=WorkspaceRole, default=WorkspaceRole.VIEWER)

    class Meta:
        constraints = (
            models.UniqueConstraint(fields=("tenant", "user"), name="%(class)s_tenant_user_uniq"),
            models.UniqueConstraint(
                fields=("tenant",),
                condition=models.Q(role=WorkspaceRole.ADMIN),
                name="%(class)s_tenant_administrator_uniq",
            ),
            choices_check("role", WorkspaceRole),
        )


class WorkspaceInvitationQuerySet[M: WorkspaceInvitation = WorkspaceInvitation, Row = M](
    models.QuerySet[M, Row]
):
    def open(self) -> Self:
        return self.filter(accepted_at__isnull=True, revoked_at__isnull=True)

    def pending(self, now: datetime) -> Self:
        return self.open().filter(expires_at__gt=now)

    def revoke(self, now: datetime, reason: InvitationRevocation) -> int:
        return self.open().update(revoked_at=now, revoked_reason=reason)


class WorkspaceInvitation(models.Model):
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    email = models.EmailField()
    # The digest of the newest token the mailer minted; none until it first prepares the mail.
    token_hash = models.CharField(max_length=64, unique=True, null=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    # The natural deadline; closing an invitation never moves it.
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True)
    accepted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, related_name="+"
    )
    revoked_at = models.DateTimeField(null=True)
    # Empty on rows revoked before reasons were stored.
    revoked_reason = models.CharField(
        max_length=21, choices=InvitationRevocation, blank=True, default=""
    )
    delivery_status = models.CharField(
        max_length=9, choices=InvitationDeliveryStatus, default=InvitationDeliveryStatus.PENDING
    )
    role = models.CharField(max_length=6, choices=WorkspaceRole, default=WorkspaceRole.VIEWER)

    objects = WorkspaceInvitationQuerySet.as_manager()

    class Meta:
        constraints = (
            choices_check("delivery_status", InvitationDeliveryStatus),
            choices_check("role", WorkspaceRole),
            choices_check("revoked_reason", InvitationRevocation, blank=True),
            models.CheckConstraint(
                condition=models.Q(revoked_at__isnull=True, revoked_reason="")
                | models.Q(revoked_at__isnull=False, accepted_at__isnull=True),
                name="%(class)s_revoked_at_chk",
            ),
            models.CheckConstraint(
                condition=models.Q(accepted_by__isnull=True) | models.Q(accepted_at__isnull=False),
                name="%(class)s_accepted_by_chk",
            ),
            models.UniqueConstraint(
                fields=("tenant", "email"),
                condition=models.Q(accepted_at__isnull=True, revoked_at__isnull=True),
                name="%(class)s_tenant_email_uniq",
            ),
            models.UniqueConstraint(
                fields=("tenant",),
                condition=models.Q(
                    role=WorkspaceRole.ADMIN, accepted_at__isnull=True, revoked_at__isnull=True
                ),
                name="%(class)s_tenant_administrator_uniq",
            ),
        )

    def state_at(self, now: datetime) -> InvitationState:
        if self.accepted_at is not None:
            return InvitationState.ACCEPTED
        if self.revoked_reason == InvitationRevocation.SUPERSEDED:
            return InvitationState.SUPERSEDED
        if self.revoked_at is not None:
            return InvitationState.REVOKED
        return InvitationState.PENDING if self.expires_at > now else InvitationState.EXPIRED


class WorkspaceAdminChange(models.Model):
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="workspace_admin_changes"
    )
    administrator = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="workspace_admin_assignments",
    )
    previous_administrator = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        related_name="previous_workspace_admin_assignments",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = (
            models.CheckConstraint(
                condition=~models.Q(administrator=models.F("previous_administrator")),
                name="%(class)s_administrator_chk",
            ),
        )


class WorkspaceMemberRemoval(models.Model):
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="workspace_member_removals"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="workspace_removals"
    )
    created_at = models.DateTimeField(auto_now_add=True)


class AuthenticationCacheEntry(models.Model):
    cache_key = models.CharField(max_length=255, primary_key=True)
    value = models.JSONField()
    expires = models.DateTimeField(null=True, db_index=True)

    class Meta:
        db_table = "pyracast_authentication_cache"


class UserLanguagePreference(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    language = models.CharField(max_length=2, choices=Language)

    class Meta:
        constraints = (choices_check("language", Language),)


class OutboundMailKind(models.TextChoices):
    WORKSPACE_INVITATION = "workspace_invitation", "Workspace invitation"
    EMAIL_CONFIRMATION_SIGNUP = "account/email/email_confirmation_signup", "Sign-up code"
    EMAIL_CONFIRMATION = "account/email/email_confirmation", "Verification code"
    PASSWORD_RESET_CODE = "account/email/password_reset_code", "Password reset code"
    LOGIN_CODE = "account/email/login_code", "Confirmation code"
    PASSWORD_CHANGED = "account/email/password_changed", "Password changed"
    PASSWORD_SET = "account/email/password_set", "Password set"
    PASSWORD_RESET = "account/email/password_reset", "Password reset"
    EMAIL_CHANGED = "account/email/email_changed", "Email address changed"
    EMAIL_CONFIRM = "account/email/email_confirm", "Email address verified"
    EMAIL_DELETED = "account/email/email_deleted", "Email address removed"
    TOTP_ACTIVATED = "mfa/email/totp_activated", "Two-factor authentication on"
    TOTP_DEACTIVATED = "mfa/email/totp_deactivated", "Two-factor authentication off"
    SECOND_FACTOR_RESET = "account/email/second_factor_reset", "Second factor reset"
    RECOVERY_CODES_GENERATED = "mfa/email/recovery_codes_generated", "Recovery codes generated"
    ACCOUNT_CONNECTED = "socialaccount/email/account_connected", "Sign-in provider connected"
    ACCOUNT_DISCONNECTED = (
        "socialaccount/email/account_disconnected",
        "Sign-in provider disconnected",
    )
    UNKNOWN_ACCOUNT = "account/email/unknown_account", "Account not found"
    ACCOUNT_ALREADY_EXISTS = "account/email/account_already_exists", "Account already exists"
    LOGIN_LOCKED = "account/email/login_locked", "Password sign-in paused"
    PLANT_APPROVED = "plant_approved", "Plant approved"
    PLANT_REJECTED = "plant_rejected", "Plant rejected"
    PLANT_SUSPENDED = "plant_suspended", "Plant suspended"
    WORKSPACE_ADMIN_ASSIGNED = "workspace_admin_assigned", "Administrator role received"
    WORKSPACE_ADMIN_RELEASED = "workspace_admin_released", "Administrator role moved away"
    PURGE_WARNING = "plant_purge_warning", "Plant deletion warning"


# Mail about a workspace event: its context names only the row it reports (`subject_id`), and
# the mailer renders it from that row while the recipient is still an active member.
EVENT_MAIL = (
    OutboundMailKind.PLANT_APPROVED,
    OutboundMailKind.PLANT_REJECTED,
    OutboundMailKind.PLANT_SUSPENDED,
    OutboundMailKind.WORKSPACE_ADMIN_ASSIGNED,
    OutboundMailKind.WORKSPACE_ADMIN_RELEASED,
    OutboundMailKind.PURGE_WARNING,
)
CODED_MAIL = (
    OutboundMailKind.EMAIL_CONFIRMATION_SIGNUP,
    OutboundMailKind.EMAIL_CONFIRMATION,
    OutboundMailKind.PASSWORD_RESET_CODE,
    OutboundMailKind.LOGIN_CODE,
)
# Anyone can trigger these for any address, so the worker sends them within one shared budget.
ANONYMOUS_MAIL = (OutboundMailKind.UNKNOWN_ACCOUNT, OutboundMailKind.ACCOUNT_ALREADY_EXISTS)


class OutboundMailStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    RETRY = "retry", "Retry"
    SENDING = "sending", "Sending"
    SENT = "sent", "Sent"
    FAILED = "failed", "Failed"
    UNCERTAIN = "uncertain", "Uncertain"
    SKIPPED = "skipped", "Skipped"


OPEN_MAIL = (OutboundMailStatus.PENDING, OutboundMailStatus.RETRY, OutboundMailStatus.SENDING)


class OutboundMailError(models.TextChoices):
    EXPIRED = "expired", "Expired before sending"
    BUDGET_EXHAUSTED = "budget_exhausted", "Hourly budget exhausted"
    INVITATION_UNAVAILABLE = "invitation_unavailable", "Invitation closed"
    NO_ACCOUNT = "no_account", "No account"
    SUBJECT_UNAVAILABLE = "subject_unavailable", "Reported item no longer available"
    RECIPIENT_UNAVAILABLE = "recipient_unavailable", "Recipient no longer a member"
    INTERRUPTED_SEND = "interrupted_send", "Interrupted during sending"
    INTERRUPTED_CONNECTION = "interrupted_connection", "Interrupted before sending"
    MAIL_NOT_SENT = MailFailure.NOT_SENT, "Mail not sent"
    MAIL_CONFIGURATION_INVALID = MailFailure.CONFIGURATION_INVALID, "Mail configuration invalid"
    MAIL_CREDENTIALS_REJECTED = MailFailure.CREDENTIALS_REJECTED, "Mail credentials rejected"
    MAIL_AUTHORIZATION_REVOKED = MailFailure.AUTHORIZATION_REVOKED, "Mail authorization revoked"
    MAIL_RATE_LIMITED = MailFailure.RATE_LIMITED, "Mail rate limited"
    MAIL_REJECTED = MailFailure.REJECTED, "Mail rejected"
    MAIL_SENDER_FORBIDDEN = MailFailure.SENDER_FORBIDDEN, "Mail sender forbidden"
    MAIL_PREPARATION_FAILED = MailFailure.PREPARATION_FAILED, "Mail preparation failed"
    MAIL_OUTCOME_UNCERTAIN = MailFailure.OUTCOME_UNCERTAIN, "Mail outcome uncertain"


class OutboundMail(models.Model):
    kind = models.CharField(max_length=48, choices=OutboundMailKind)
    # The address the request named; the daily-lock notice goes to its account's primary one.
    # As long as the account inputs accept (Django's `EmailValidator`), longer than an account's.
    recipient = models.EmailField(max_length=320)
    invitation = models.ForeignKey(WorkspaceInvitation, on_delete=models.CASCADE, null=True)
    language = models.CharField(max_length=2, choices=Language)
    # Set when no signed-in account asked for the mail: the worker then uses the language of the
    # address's account, so the request never looks the account up.
    owner_language = models.BooleanField(default=False)
    context = models.JSONField(default=dict)
    # A one-time code sealed by `accounts.mail_intents.seal_code`; erased when the intent ends.
    sealed_code = models.TextField(blank=True, default="")
    status = models.CharField(
        max_length=9, choices=OutboundMailStatus, default=OutboundMailStatus.PENDING
    )
    created_at = models.DateTimeField()
    available_at = models.DateTimeField()
    expires_at = models.DateTimeField()
    published_at = models.DateTimeField(null=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    lease_token = models.UUIDField(null=True)
    lease_expires_at = models.DateTimeField(null=True)
    transmit_started_at = models.DateTimeField(null=True)
    finished_at = models.DateTimeField(null=True)
    last_error = models.CharField(max_length=30, choices=OutboundMailError, blank=True, default="")

    class Meta:
        # The notifications app keeps this table and its migrations; account flows write the
        # intents in their own transactions, so the model is defined beside them.
        app_label = "notifications"
        constraints = (
            choices_check("kind", OutboundMailKind),
            choices_check("status", OutboundMailStatus),
            choices_check("language", Language),
            choices_check("last_error", OutboundMailError, blank=True),
            lease_check((OutboundMailStatus.SENDING,), kept=(OutboundMailStatus.UNCERTAIN,)),
            models.CheckConstraint(
                condition=models.Q(sealed_code="")
                | models.Q(kind__in=CODED_MAIL, status__in=OPEN_MAIL),
                name="%(class)s_sealed_code_chk",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    kind=OutboundMailKind.WORKSPACE_INVITATION, invitation__isnull=False
                )
                | (
                    ~models.Q(kind=OutboundMailKind.WORKSPACE_INVITATION)
                    & models.Q(invitation__isnull=True)
                ),
                name="%(class)s_invitation_chk",
            ),
            json_shape_check("context", "object", CONTEXT_ITEMS),
        )
        indexes = (
            models.Index(
                fields=("available_at", "id"),
                condition=models.Q(status__in=OPEN_MAIL),
                name="%(class)s_due_idx",
            ),
        )

    def __str__(self) -> str:
        return f"Outbound mail {self.pk} ({self.kind}): {self.status}"
