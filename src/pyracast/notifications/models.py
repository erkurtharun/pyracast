from django.conf import settings
from django.db import models

from pyracast.forecasts.models import ForecastSnapshot
from pyracast.regions.models import Region


class ReminderPreference(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    region = models.ForeignKey(Region, on_delete=models.CASCADE)
    enabled = models.BooleanField(default=False)
    email = models.EmailField(blank=True, default="")
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
    sent_at = models.DateTimeField(null=True)
    last_error = models.CharField(max_length=40, default="")

    class Meta:
        constraints = (
            models.UniqueConstraint(
                fields=("preference", "scheduled_for"), name="unique_reminder_delivery_slot"
            ),
        )
