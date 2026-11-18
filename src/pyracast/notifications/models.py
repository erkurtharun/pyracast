import uuid

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models

from pyracast.accounts.models import Language
from pyracast.connections.models import SiteBinding
from pyracast.db import choices_check
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
        )


PAUSED_DELIVERY_ERRORS = ("recipient_limit_exceeded", "no_recipients", "invalid_recipient")


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
    last_error = models.CharField(max_length=40, default="")
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
            models.CheckConstraint(
                condition=~models.Q(status=NotificationDeliveryStatus.SENDING)
                | models.Q(prepared_message__isnull=False),
                name="notification_delivery_sending_prepared",
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
                name="notification_dispatch_ready",
            ),
        )
