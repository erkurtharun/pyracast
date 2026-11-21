import uuid

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.utils.translation import gettext_lazy

from pyracast.accounts.models import Language
from pyracast.connections.models import SiteBinding
from pyracast.db import TIME_ITEMS, choices_check, json_shape_check, lease_check
from pyracast.forecasts.models import ForecastSnapshot
from pyracast.regions.models import Region


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
    revision = models.PositiveIntegerField(default=0)

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
        ReminderSchedule, on_delete=models.CASCADE, related_name="recipient_rows"
    )
    address = models.TextField()
    # `recipients.recipient_key(address)`: the identity used for uniqueness, removal and counts.
    key = models.TextField()

    class Meta:
        ordering = ("pk",)
        constraints = (
            models.UniqueConstraint(fields=("schedule", "key"), name="%(class)s_schedule_key_uniq"),
        )

    def __str__(self) -> str:
        return f"Recipient {self.pk} of schedule {self.schedule_id}"


class ReminderRecipientTie(models.Model):
    recipient = models.ForeignKey(ReminderRecipient, on_delete=models.CASCADE, related_name="ties")
    # A plain id, not a foreign key: a tie to a removed membership keeps its address ineligible.
    membership_id = models.PositiveBigIntegerField()

    class Meta:
        constraints = (
            models.UniqueConstraint(
                fields=("recipient", "membership_id"), name="%(class)s_recipient_membership_uniq"
            ),
        )

    def __str__(self) -> str:
        return f"Recipient {self.recipient_id} tied to membership {self.membership_id}"


class NotificationDeliveryError(models.TextChoices):
    MISSED_SCHEDULE = "missed_schedule", gettext_lazy("Missed schedule")
    DELIVERY_DEADLINE_EXCEEDED = (
        "delivery_deadline_exceeded",
        gettext_lazy("Delivery deadline exceeded"),
    )
    CANCELLED_BY_SETTINGS_CHANGE = (
        "cancelled_by_settings_change",
        gettext_lazy("Cancelled by settings change"),
    )
    SCHEDULE_OR_AUTHORITY_CHANGED = (
        "schedule_or_authority_changed",
        gettext_lazy("Schedule or authority changed"),
    )
    NO_RECIPIENTS = "no_recipients", gettext_lazy("No recipients")
    INVALID_RECIPIENT = "invalid_recipient", gettext_lazy("Invalid recipient")
    RECIPIENT_LIMIT_EXCEEDED = "recipient_limit_exceeded", gettext_lazy("Recipient limit exceeded")
    DEFINITION_TRASHED = "definition_trashed", gettext_lazy("Definition trashed")
    INTERRUPTED_SEND = "interrupted_send", gettext_lazy("Interrupted during sending")
    INTERRUPTED_CONNECTION = "interrupted_connection", gettext_lazy("Interrupted before sending")
    # Every MailFailure code is a member: send failures store error.code here.
    MAIL_NOT_SENT = "mail_not_sent", gettext_lazy("Mail not sent")
    MAIL_CONFIGURATION_INVALID = (
        "mail_configuration_invalid",
        gettext_lazy("Mail configuration invalid"),
    )
    MAIL_CREDENTIALS_REJECTED = (
        "mail_credentials_rejected",
        gettext_lazy("Mail credentials rejected"),
    )
    MAIL_AUTHORIZATION_REVOKED = (
        "mail_authorization_revoked",
        gettext_lazy("Mail authorization revoked"),
    )
    MAIL_RATE_LIMITED = "mail_rate_limited", gettext_lazy("Mail rate limited")
    MAIL_REJECTED = "mail_rejected", gettext_lazy("Mail rejected")
    MAIL_SENDER_FORBIDDEN = "mail_sender_forbidden", gettext_lazy("Mail sender forbidden")
    MAIL_PREPARATION_FAILED = "mail_preparation_failed", gettext_lazy("Mail preparation failed")
    MAIL_OUTCOME_UNCERTAIN = "mail_outcome_uncertain", gettext_lazy("Mail outcome uncertain")


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
    PENDING = "pending", gettext_lazy("Pending")
    RETRY = "retry", gettext_lazy("Retry")
    SENDING = "sending", gettext_lazy("Sending")
    SENT = "sent", gettext_lazy("Sent")
    FAILED = "failed", gettext_lazy("Failed")
    UNCERTAIN = "uncertain", gettext_lazy("Uncertain")
    SKIPPED = "skipped", gettext_lazy("Skipped")
    CANCELLED = "cancelled", gettext_lazy("Cancelled")


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
    # The rendered mail; the retention sweep clears it on ended deliveries, the columns below stay.
    prepared_message = models.JSONField(null=True)
    forecast_kind = models.CharField(
        max_length=14, choices=PreparedMessageKind, blank=True, default=""
    )
    # Whether tomorrow's energy was complete; unknown for an unavailable forecast.
    forecast_complete = models.BooleanField(null=True)
    prepared_at = models.DateTimeField(null=True)
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
            choices_check("forecast_kind", PreparedMessageKind, blank=True),
            models.CheckConstraint(
                condition=models.Q(
                    forecast_kind="", forecast_complete__isnull=True, prepared_at__isnull=True
                )
                | (~models.Q(forecast_kind="") & models.Q(prepared_at__isnull=False)),
                name="%(class)s_prepared_at_chk",
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


class DeliveryRecipient(models.Model):
    """A candidate address of one delivery. The claim and the transmit marker delete the rows they
    exclude, so after the marker the rows are the envelope; a later member removal only sets
    `revoked`, which keeps the address out of later attempts."""

    delivery = models.ForeignKey(
        NotificationDelivery, on_delete=models.CASCADE, related_name="recipient_rows"
    )
    address = models.TextField()
    key = models.TextField()
    revoked = models.BooleanField(default=False)

    class Meta:
        ordering = ("pk",)
        constraints = (
            models.UniqueConstraint(fields=("delivery", "key"), name="%(class)s_delivery_key_uniq"),
        )

    def __str__(self) -> str:
        return f"Recipient {self.pk} of delivery {self.delivery_id}"


class ReminderState(models.TextChoices):
    SENDING = "sending", "Sending"
    PAUSED_BY_USER = "paused_by_user", "Paused"
    WAITING_FOR_APPROVAL = "waiting_for_approval", "Waiting for approval"
    PROVIDER_ATTENTION = "provider_attention", "Provider account needs attention"
    NO_RECIPIENTS = "no_recipients", "No recipients"
    INVALID_RECIPIENT = "invalid_recipient", "Invalid recipient"
    OVER_RECIPIENT_LIMIT = "over_recipient_limit", "Over the recipient limit"
