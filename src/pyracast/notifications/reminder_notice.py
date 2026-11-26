from datetime import timedelta
from zoneinfo import ZoneInfo

from django.utils.translation import gettext as _

from pyracast.notifications.event_messages import plant_name
from pyracast.notifications.links import plant_url
from pyracast.notifications.models import NotificationDelivery
from pyracast.notifications.reminder_slot import edition, missed_collection
from pyracast.notifications.rendering import (
    ForecastMail,
    no_forecast_sentence,
    no_forecast_subject,
)


def reminder_headers(language: str) -> dict[str, str]:
    return {
        "Auto-Submitted": "auto-generated",
        "X-Auto-Response-Suppress": "OOF, AutoReply",
        "Content-Language": language,
    }


def reminder_footer(delivery: NotificationDelivery) -> str:
    region = delivery.schedule.region
    return _(
        "Forecast email of %(plant)s in %(workspace)s. The workspace's administrators manage "
        "who receives it."
    ) % {"workspace": region.tenant.name, "plant": plant_name(region)}


def forecast_mail(delivery: NotificationDelivery) -> ForecastMail:
    return ForecastMail(
        footer=reminder_footer(delivery),
        plant_url=plant_url(delivery.schedule.region),
        edition=edition(delivery),
        missed_collection=missed_collection(delivery),
    )


def unavailable_text(delivery: NotificationDelivery) -> tuple[str, str]:
    """Subject and body of the notice sent when preparation failed, composed without templates
    so that a broken template cannot suppress it; the caller holds the delivery's language."""
    region = delivery.schedule.region
    tomorrow = delivery.scheduled_for.astimezone(ZoneInfo(region.timezone)).date() + timedelta(
        days=1
    )
    body = "\n".join(
        (
            no_forecast_sentence(tomorrow),
            "",
            f"{_('Open plant in Pyracast')}:",
            plant_url(region),
            "",
            "--",
            reminder_footer(delivery),
        )
    )
    return no_forecast_subject(plant_name(region), tomorrow, edition(delivery)), body
