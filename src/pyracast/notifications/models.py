import uuid

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models.functions import MD5, Lower

from pyracast.accounts.models import Language, Membership, WorkspaceInvitation
from pyracast.connections.models import SiteBinding
from pyracast.db import TIME_ITEMS, choices_check, json_shape_check, lease_check
from pyracast.forecasts.models import ForecastSnapshot
from pyracast.mail.errors import MailFailure
from pyracast.regions.models import Region

# Template values an outbound mail may keep: strings only, never a code, key or link.
MAIL_CONTEXT_KEYS = (
    "from_email",
    "to_email",
    "deleted_email",
    "provider",
    "timestamp",
    "ip",
    "user_agent",
)
CONTEXT_ITEMS = (
    'strict $.keyvalue() ? (@.value.type() != "string"'
    f' || !(@.key like_regex "^({"|".join(MAIL_CONTEXT_KEYS)})$"))'
)


class NotificationSettings(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    max_recipients = models.PositiveSmallIntegerField(
        default=5,
        validators=[MinValueValidator(1)],
        help_text=(
            "Global recipient limit per plant reminder schedule. Lowering it pauses reminders "
            "at plants exceeding the limit until their recipient lists are revised."
        ),
    )

    class Meta:
        verbose_name = "email reminder settings"
        verbose_name_plural = "email reminder settings"
        constraints = (
            models.CheckConstraint(condition=models.Q(pk=1), name="%(class)s_id_chk"),
            models.CheckConstraint(
                condition=models.Q(max_recipients__gte=1), name="%(class)s_max_recipients_chk"
            ),
        )

    def __str__(self) -> str:
        return "Email reminder settings"


class ReminderSchedule(models.Model):
    configured_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True
    )
    region = models.ForeignKey(Region, on_delete=models.CASCADE)
    enabled = models.BooleanField(default=False)
    include_workspace_members = models.BooleanField(default=False)
    times = models.JSONField(default=list)
    effective_from = models.DateTimeField()
    language = models.CharField(max_length=2, choices=Language, default=Language.EN)

    class Meta:
        constraints = (
            models.UniqueConstraint(fields=("region",), name="%(class)s_region_uniq"),
            choices_check("language", Language),
            json_shape_check("times", "array", TIME_ITEMS),
        )

    def __str__(self) -> str:
        return f"Reminders of region {self.region_id}"


class ReminderRecipient(models.Model):
    schedule = models.ForeignKey(
        ReminderSchedule, on_delete=models.CASCADE, related_name="recipients"
    )
    # Unbounded so an invalid saved address stays visible as one; input is validated to 254.
    email = models.CharField()
    # One row for each member the address belonged to at saving, each going with its member;
    # the address stays a recipient while any of them is active. None: no member's address.
    membership = models.ForeignKey(Membership, on_delete=models.CASCADE, null=True)

    class Meta:
        ordering = ("pk",)
        constraints = (
            # The digest keeps every address indexable, whatever its length.
            models.UniqueConstraint(
                "schedule",
                MD5(Lower("email")),
                "membership",
                nulls_distinct=False,
                name="%(class)s_schedule_email_uniq",
            ),
        )

    def __str__(self) -> str:
        return self.email


class NotificationDeliveryError(models.TextChoices):
    MISSED_SCHEDULE = "missed_schedule", "Missed schedule"
    DELIVERY_DEADLINE_EXCEEDED = "delivery_deadline_exceeded", "Delivery deadline exceeded"
    CANCELLED_BY_SETTINGS_CHANGE = "cancelled_by_settings_change", "Cancelled by settings change"
    SCHEDULE_OR_AUTHORITY_CHANGED = (
        "schedule_or_authority_changed",
        "Schedule or authority changed",
    )
    NO_RECIPIENTS = "no_recipients", "No recipients"
    INVALID_RECIPIENT = "invalid_recipient", "Invalid recipient"
    RECIPIENT_LIMIT_EXCEEDED = "recipient_limit_exceeded", "Recipient limit exceeded"
    DEFINITION_TRASHED = "definition_trashed", "Definition trashed"
    INTERRUPTED_SEND = "interrupted_send", "Interrupted during sending"
    INTERRUPTED_CONNECTION = "interrupted_connection", "Interrupted before sending"
    # Every MailFailure code is a member: send failures store error.code here.
    MAIL_NOT_SENT = "mail_not_sent", "Mail not sent"
    MAIL_CONFIGURATION_INVALID = "mail_configuration_invalid", "Mail configuration invalid"
    MAIL_CREDENTIALS_REJECTED = "mail_credentials_rejected", "Mail credentials rejected"
    MAIL_AUTHORIZATION_REVOKED = "mail_authorization_revoked", "Mail authorization revoked"
    MAIL_RATE_LIMITED = "mail_rate_limited", "Mail rate limited"
    MAIL_REJECTED = "mail_rejected", "Mail rejected"
    MAIL_SENDER_FORBIDDEN = "mail_sender_forbidden", "Mail sender forbidden"
    MAIL_PREPARATION_FAILED = "mail_preparation_failed", "Mail preparation failed"
    MAIL_OUTCOME_UNCERTAIN = "mail_outcome_uncertain", "Mail outcome uncertain"


PAUSED_DELIVERY_ERRORS = (
    NotificationDeliveryError.RECIPIENT_LIMIT_EXCEEDED,
    NotificationDeliveryError.NO_RECIPIENTS,
    NotificationDeliveryError.INVALID_RECIPIENT,
)


class PreparedMessageFormat(models.TextChoices):
    XLSX = "xlsx", "Workbook"
    PLAIN = "plain", "Plain text"


class PreparedMessageKind(models.TextChoices):
    CURRENT = "current", "Current forecast"
    SAVED_COMPLETE = "saved_complete", "Saved complete forecast"
    SAVED_PARTIAL = "saved_partial", "Saved partial forecast"
    UNAVAILABLE = "unavailable", "Unavailable"


class NotificationDeliveryStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    RETRY = "retry", "Retry"
    SENDING = "sending", "Sending"
    SENT = "sent", "Sent"
    FAILED = "failed", "Failed"
    UNCERTAIN = "uncertain", "Uncertain"
    SKIPPED = "skipped", "Skipped"
    CANCELLED = "cancelled", "Cancelled"


class NotificationDelivery(models.Model):
    schedule = models.ForeignKey(ReminderSchedule, on_delete=models.CASCADE)
    scheduled_for = models.DateTimeField()
    status = models.CharField(
        max_length=20,
        choices=NotificationDeliveryStatus,
        default=NotificationDeliveryStatus.PENDING,
    )
    available_at = models.DateTimeField()
    attempts = models.PositiveIntegerField(default=0)
    lease_token = models.UUIDField(null=True)
    lease_expires_at = models.DateTimeField(null=True)
    language = models.CharField(max_length=2, choices=Language, default=Language.EN)
    snapshot = models.ForeignKey(ForecastSnapshot, on_delete=models.PROTECT, null=True)
    binding = models.ForeignKey(SiteBinding, on_delete=models.PROTECT)
    preparation_deadline = models.DateTimeField()
    prepared_message = models.JSONField(null=True)
    transmit_started_at = models.DateTimeField(null=True)
    sent_at = models.DateTimeField(null=True)
    last_error = models.CharField(
        max_length=40, choices=NotificationDeliveryError, blank=True, default=""
    )
    message_token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)

    class Meta:
        constraints = (
            models.UniqueConstraint(
                fields=("schedule", "scheduled_for"), name="%(class)s_schedule_scheduled_for_uniq"
            ),
            choices_check("language", Language),
            choices_check("status", NotificationDeliveryStatus),
            choices_check("last_error", NotificationDeliveryError, blank=True),
            # A NULL message or a missing key passes: PostgreSQL treats the unknown result as true.
            choices_check(
                "prepared_message__format",
                PreparedMessageFormat,
                name="notificationdelivery_prepared_format_chk",
            ),
            choices_check(
                "prepared_message__kind",
                PreparedMessageKind,
                name="notificationdelivery_prepared_kind_chk",
            ),
            models.CheckConstraint(
                condition=~models.Q(status=NotificationDeliveryStatus.SENDING)
                | models.Q(prepared_message__isnull=False),
                name="%(class)s_prepared_message_chk",
            ),
            # An uncertain delivery keeps the lease of the send whose late outcome it awaits.
            lease_check(
                (NotificationDeliveryStatus.SENDING,), kept=(NotificationDeliveryStatus.UNCERTAIN,)
            ),
        )
        indexes = (
            models.Index(
                fields=("available_at", "id"),
                condition=models.Q(
                    status__in=(
                        NotificationDeliveryStatus.PENDING,
                        NotificationDeliveryStatus.RETRY,
                        NotificationDeliveryStatus.SENDING,
                    )
                ),
                name="%(class)s_due_idx",
            ),
        )

    def __str__(self) -> str:
        return f"Delivery {self.pk} of schedule {self.schedule_id}: {self.status}"

    def addresses(self) -> list[str]:
        """Each address once: the envelope a sent or uncertain delivery was authorized for,
        else the addresses it still goes to."""
        authorized = self.status in (
            NotificationDeliveryStatus.SENT,
            NotificationDeliveryStatus.UNCERTAIN,
        )
        named: dict[str, str] = {}
        for row in self.recipients.all():
            if row.authorized if authorized else not row.revoked:
                named.setdefault(row.email.casefold(), row.email)
        return list(named.values())


class DeliveryRecipient(models.Model):
    delivery = models.ForeignKey(
        NotificationDelivery, on_delete=models.CASCADE, related_name="recipients"
    )
    email = models.CharField()
    # A membership the address belonged to when the slot was created, kept as history: one row
    # for each, the address permitted while any of them is active. None: no member's address.
    membership_id = models.PositiveBigIntegerField(null=True)
    # Excluded from every later attempt.
    revoked = models.BooleanField(default=False)
    # In the envelope of the latest transmit marker; a later revocation leaves it there.
    authorized = models.BooleanField(default=False)

    class Meta:
        ordering = ("pk",)
        constraints = (
            models.UniqueConstraint(
                "delivery",
                MD5(Lower("email")),
                "membership_id",
                nulls_distinct=False,
                name="%(class)s_delivery_email_uniq",
            ),
        )

    def __str__(self) -> str:
        return self.email


class ReminderState(models.TextChoices):
    SENDING = "sending", "Sending"
    PAUSED_BY_USER = "paused_by_user", "Paused"
    WAITING_FOR_APPROVAL = "waiting_for_approval", "Waiting for approval"
    PROVIDER_ATTENTION = "provider_attention", "Provider account needs attention"
    NO_RECIPIENTS = "no_recipients", "No recipients"
    INVALID_RECIPIENT = "invalid_recipient", "Invalid recipient"
    OVER_RECIPIENT_LIMIT = "over_recipient_limit", "Over the recipient limit"


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
    RECOVERY_CODES_GENERATED = "mfa/email/recovery_codes_generated", "Recovery codes generated"
    ACCOUNT_CONNECTED = "socialaccount/email/account_connected", "Sign-in provider connected"
    ACCOUNT_DISCONNECTED = (
        "socialaccount/email/account_disconnected",
        "Sign-in provider disconnected",
    )
    UNKNOWN_ACCOUNT = "account/email/unknown_account", "Account not found"
    ACCOUNT_ALREADY_EXISTS = "account/email/account_already_exists", "Account already exists"
    LOGIN_LOCKED = "account/email/login_locked", "Password sign-in paused"


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
    recipient = models.EmailField()
    invitation = models.ForeignKey(WorkspaceInvitation, on_delete=models.CASCADE, null=True)
    language = models.CharField(max_length=2, choices=Language)
    # Set when no signed-in account asked for the mail: the worker then uses the language of the
    # address's account, so the request never looks the account up.
    owner_language = models.BooleanField(default=False)
    context = models.JSONField(default=dict)
    # A one-time code sealed by `notifications.intents.seal_code`; erased when the intent ends.
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
    # The daily lock a sign-in pause notice announces, so each lock is stored once.
    lock_key = models.CharField(max_length=64, null=True)

    class Meta:
        constraints = (
            choices_check("kind", OutboundMailKind),
            choices_check("status", OutboundMailStatus),
            choices_check("language", Language),
            choices_check("last_error", OutboundMailError, blank=True),
            lease_check((OutboundMailStatus.SENDING,), kept=(OutboundMailStatus.UNCERTAIN,)),
            models.UniqueConstraint(fields=("lock_key",), name="%(class)s_lock_key_uniq"),
            models.CheckConstraint(
                condition=models.Q(kind=OutboundMailKind.LOGIN_LOCKED, lock_key__isnull=False)
                | (~models.Q(kind=OutboundMailKind.LOGIN_LOCKED) & models.Q(lock_key__isnull=True)),
                name="%(class)s_lock_key_chk",
            ),
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
