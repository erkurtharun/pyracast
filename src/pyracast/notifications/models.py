import uuid

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models

from pyracast.accounts.models import Language
from pyracast.connections.models import SiteBinding
from pyracast.db import TEXT_ITEMS, choices_check, json_shape_check, lease_check
from pyracast.forecasts.models import ForecastSnapshot
from pyracast.regions.models import Region

# A pinned address maps to the ids of the memberships it belonged to.
MEMBERSHIP_ITEMS = (
    'strict $.* ? (@.type() != "array"'
    ' || exists(@[*] ? (@.type() != "number" || @ < 1 || @.floor() != @)))'
)
TIME_ITEMS = (
    'strict $[*] ? (@.type() != "string" || !(@ like_regex "^([01][0-9]|2[0-3]):[0-5][0-9]$"))'
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
            models.CheckConstraint(
                condition=models.Q(pk=1), name="singleton_notification_settings"
            ),
            models.CheckConstraint(
                condition=models.Q(max_recipients__gte=1),
                name="positive_notification_recipient_limit",
            ),
        )


class ReminderSchedule(models.Model):
    configured_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True
    )
    region = models.ForeignKey(Region, on_delete=models.CASCADE)
    enabled = models.BooleanField(default=False)
    recipients = models.JSONField(default=list)
    include_workspace_members = models.BooleanField(default=False)
    recipient_memberships = models.JSONField(default=dict)
    times = models.JSONField(default=list)
    effective_from = models.DateTimeField()
    language = models.CharField(max_length=2, choices=Language, default=Language.EN)

    class Meta:
        constraints = (
            models.UniqueConstraint(fields=("region",), name="one_reminder_schedule_per_region"),
            choices_check("language", Language, name="reminder_schedule_language_valid"),
            json_shape_check("recipients", "array", TEXT_ITEMS),
            json_shape_check("recipient_memberships", "object", MEMBERSHIP_ITEMS),
            json_shape_check("times", "array", TIME_ITEMS),
        )


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
    recipients = models.JSONField(default=list)
    recipient_memberships = models.JSONField(default=dict)

    revoked_recipients = models.JSONField(default=list)
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
                fields=("schedule", "scheduled_for"), name="unique_reminder_group_slot"
            ),
            choices_check("language", Language, name="notification_delivery_language_valid"),
            choices_check(
                "status", NotificationDeliveryStatus, name="notification_delivery_status_valid"
            ),
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
                name="notification_delivery_sending_prepared",
            ),
            # An uncertain delivery keeps the lease of the send whose late outcome it awaits.
            lease_check(
                (NotificationDeliveryStatus.SENDING,), kept=(NotificationDeliveryStatus.UNCERTAIN,)
            ),
            json_shape_check("recipients", "array", TEXT_ITEMS),
            json_shape_check("recipient_memberships", "object", MEMBERSHIP_ITEMS),
            json_shape_check("revoked_recipients", "array", TEXT_ITEMS),
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
                name="notification_dispatch_ready",
            ),
        )
