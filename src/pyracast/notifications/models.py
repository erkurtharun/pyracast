from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models

from pyracast.connections.models import SiteBinding
from pyracast.forecasts.models import ForecastSnapshot
from pyracast.regions.models import Region


class NotificationSettings(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    max_recipients = models.PositiveSmallIntegerField(
        default=5,
        validators=[MinValueValidator(1)],
        help_text=(
            "Global distinct recipient limit per plant across all members. Lowering it pauses "
            "reminders at plants exceeding the limit until their recipient lists are revised."
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


class ReminderPreference(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    region = models.ForeignKey(Region, on_delete=models.CASCADE)
    enabled = models.BooleanField(default=False)
    recipients = models.JSONField(default=list)
    times = models.JSONField(default=list)
    effective_from = models.DateTimeField()

    class Meta:
        constraints = (
            models.UniqueConstraint(fields=("user", "region"), name="unique_user_region_reminder"),
        )


class NotificationDelivery(models.Model):
    preference = models.ForeignKey(ReminderPreference, on_delete=models.CASCADE)
    scheduled_for = models.DateTimeField()
    status = models.CharField(max_length=20, default="pending")
    available_at = models.DateTimeField()
    attempts = models.PositiveIntegerField(default=0)
    lease_token = models.UUIDField(null=True)
    lease_expires_at = models.DateTimeField(null=True)
    recipient = models.EmailField(blank=True, default="")
    snapshot = models.ForeignKey(ForecastSnapshot, on_delete=models.PROTECT, null=True)
    binding = models.ForeignKey(SiteBinding, on_delete=models.PROTECT, null=True)
    preparation_deadline = models.DateTimeField(null=True)
    prepared_message = models.JSONField(null=True)
    transmit_started_at = models.DateTimeField(null=True)
    sent_at = models.DateTimeField(null=True)
    last_error = models.CharField(max_length=40, default="")

    class Meta:
        constraints = (
            models.UniqueConstraint(
                fields=("preference", "scheduled_for", "recipient"),
                name="unique_reminder_recipient_slot",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    status__in=(
                        "pending",
                        "retry",
                        "sending",
                        "sent",
                        "failed",
                        "uncertain",
                        "skipped",
                        "unavailable",
                        "cancelled",
                    )
                ),
                name="notification_delivery_status_valid",
            ),
        )
        indexes = (
            models.Index(
                fields=("available_at", "id"),
                condition=models.Q(status__in=("pending", "retry", "sending")),
                name="notification_dispatch_ready",
            ),
        )
