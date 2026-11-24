from datetime import timedelta
from email.utils import parseaddr
from zoneinfo import ZoneInfo

from django.conf import settings
from django.utils.translation import gettext as _

from pyracast.notifications.event_messages import plant_name
from pyracast.notifications.links import plant_url
from pyracast.notifications.models import NotificationDelivery
from pyracast.notifications.reminder_slot import edition, missed_collection
from pyracast.notifications.rendering import ForecastMail, render_no_forecast


def reminder_headers(language: str) -> dict[str, str]:
    """`List-Unsubscribe` names the sender's mailbox only: one message reaches every recipient."""
    address = parseaddr(settings.DEFAULT_FROM_EMAIL)[1]
    return {
        "List-Unsubscribe": f"<mailto:{address}?subject=unsubscribe>",
        "Auto-Submitted": "auto-generated",
        "X-Auto-Response-Suppress": "OOF, AutoReply",
        "Content-Language": language,
    }


def reminder_footer(delivery: NotificationDelivery) -> str:
    region = delivery.schedule.region
    return _(
        "You receive this email because an administrator of %(workspace)s added your "
        "address to the forecast emails of %(plant)s. To stop receiving it, ask a workspace "
        "administrator to remove your address."
    ) % {"workspace": region.tenant.name, "plant": plant_name(region)}


def forecast_mail(delivery: NotificationDelivery) -> ForecastMail:
    return ForecastMail(
        footer=reminder_footer(delivery),
        plant_url=plant_url(delivery.schedule.region),
        edition=edition(delivery),
        missed_collection=missed_collection(delivery),
    )


def unavailable_text(delivery: NotificationDelivery) -> tuple[str, str]:
    """Subject and body of the notice sent when preparation failed; the caller holds the
    delivery's language."""
    region = delivery.schedule.region
    tomorrow = delivery.scheduled_for.astimezone(ZoneInfo(region.timezone)).date() + timedelta(
        days=1
    )
    mail = ForecastMail(
        footer=reminder_footer(delivery), plant_url=plant_url(region), edition=edition(delivery)
    )
    rendered = render_no_forecast(plant_name(region), tomorrow, mail, kind="", heading="")
    return rendered["subject"], rendered["body"]
