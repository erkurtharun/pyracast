from datetime import timedelta
from email.utils import parseaddr
from zoneinfo import ZoneInfo

from django.conf import settings
from django.utils.formats import date_format
from django.utils.translation import gettext as _

from pyracast.notifications.event_messages import plant_name
from pyracast.notifications.links import plant_url
from pyracast.notifications.models import NotificationDelivery


def unsubscribe_header() -> dict[str, str]:
    """One message reaches every recipient in BCC, so the header names only the sender's
    mailbox, where a reply asks the workspace to stop the reminders."""
    address = parseaddr(settings.DEFAULT_FROM_EMAIL)[1]
    return {"List-Unsubscribe": f"<mailto:{address}?subject=unsubscribe>"}


def reminder_footer(delivery: NotificationDelivery) -> str:
    region = delivery.schedule.region
    return _(
        "You receive this email because an administrator of %(workspace)s added your "
        "address to the reminders of %(plant)s. To stop them, ask that administrator to "
        "remove your address."
    ) % {"workspace": region.tenant.name, "plant": plant_name(region)}


def unavailable_text(delivery: NotificationDelivery) -> tuple[str, str]:
    """Subject and body of the notice sent when no forecast could be prepared, in plant time;
    the caller holds the delivery's language."""
    region = delivery.schedule.region
    local = delivery.scheduled_for.astimezone(ZoneInfo(region.timezone))
    values = {
        "plant": plant_name(region),
        "date": date_format(local.date() + timedelta(days=1), "j M Y"),
        "time": date_format(local, "j M Y H:i"),
        "zone": region.timezone,
        "url": plant_url(region),
    }
    subject = _("%(plant)s: forecast unavailable for %(date)s") % values
    body = (
        _(
            "Tomorrow's saved forecast for %(plant)s could not be prepared. No forecast energy "
            "total is available.\n"
            "Scheduled reminder: %(time)s (%(zone)s)\n"
            "Check the plant page for collection status: %(url)s"
        )
        % values
    )
    return " ".join(subject.split()), f"{body}\n\n{reminder_footer(delivery)}"
