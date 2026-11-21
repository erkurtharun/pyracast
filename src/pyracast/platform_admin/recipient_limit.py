from dataclasses import dataclass

from django.urls import reverse

from pyracast.notifications.models import ReminderSchedule
from pyracast.notifications.recipients import (
    NO_MEMBERS,
    effective,
    explicit_recipients,
    needs_members,
    workspaces,
)
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
    reminders pause at that limit, counted as the scheduler counts them, in a fixed number
    of queries."""
    names = dict(Plant.objects.values_list("region_id", "name"))
    schedules = list(
        ReminderSchedule.objects.filter(enabled=True, region__deleted_at__isnull=True)
        .select_related("region__tenant")
        .order_by("region__tenant__name", "region_id")
    )
    explicit = explicit_recipients(schedule.pk for schedule in schedules)
    members = workspaces(
        schedule.region.tenant_id
        for schedule in schedules
        if needs_members(schedule.include_workspace_members, explicit.get(schedule.pk, []))
    )
    crowded = []
    for schedule in schedules:
        region = schedule.region
        count = len(
            effective(
                schedule.include_workspace_members,
                explicit.get(schedule.pk, []),
                members.get(region.tenant_id, NO_MEMBERS),
            )
        )
        if count > limit:
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
