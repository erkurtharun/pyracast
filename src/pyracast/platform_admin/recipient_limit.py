from dataclasses import dataclass

from django.urls import reverse

from pyracast.notifications.models import ReminderSchedule
from pyracast.notifications.recipients import effective_recipients
from pyracast.plants.models import Plant


@dataclass(frozen=True)
class CrowdedPlant:
    region_id: int
    name: str
    workspace: str
    recipients: int
    review_url: str


def crowded_plants(limit: int) -> list[CrowdedPlant]:
    """Enabled reminder schedules of live plants whose recipients exceed `limit`: their
    reminders pause at that limit, counted as the scheduler counts them."""
    names = dict(Plant.objects.values_list("region_id", "name"))
    crowded = []
    for schedule in (
        ReminderSchedule.objects.filter(enabled=True, region__deleted_at__isnull=True)
        .select_related("region__tenant")
        .order_by("region__tenant__name", "region_id")
    ):
        count = len(effective_recipients(schedule))
        if count > limit:
            region = schedule.region
            crowded.append(
                CrowdedPlant(
                    region.pk,
                    names.get(region.pk, region.name),
                    region.tenant.name,
                    count,
                    reverse("admin:workspace_plant", args=(region.pk,)),
                )
            )
    return crowded


def newly_paused(current: int, proposed: int) -> list[CrowdedPlant]:
    """The plants a change from `current` to `proposed` would pause that are not paused now."""
    paused = {plant.region_id for plant in crowded_plants(current)}
    return [plant for plant in crowded_plants(proposed) if plant.region_id not in paused]
