from datetime import datetime

from django.db import transaction

from pyracast.notifications.models import NotificationDelivery
from pyracast.notifications.recipients import OPEN_DELIVERIES


def trim_rendered_mail(cutoff: datetime, limit: int) -> int:
    """Clears the rendered mail of ended deliveries scheduled before the cutoff; the delivery,
    its typed metadata and its recipients stay."""
    with transaction.atomic():
        ids = list(
            NotificationDelivery.objects.select_for_update(skip_locked=True)
            .exclude(status__in=OPEN_DELIVERIES)
            .filter(prepared_message__isnull=False, scheduled_for__lt=cutoff)
            .order_by("scheduled_for", "pk")
            .values_list("pk", flat=True)[:limit]
        )
        return NotificationDelivery.objects.filter(pk__in=ids).update(prepared_message=None)
