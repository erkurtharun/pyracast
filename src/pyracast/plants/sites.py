from collections.abc import Sequence
from dataclasses import dataclass

from django.contrib.auth.models import User
from django.db import models, transaction
from django.db.models import QuerySet

from pyracast.plants.models import Plant
from pyracast.plants.operations import PlantEditBlock, create_plant, plant_edit_block
from pyracast.plants.parameters import PlantParameters
from pyracast.regions.models import Region, RegionStatus
from pyracast.regions.operations import submit_region
from pyracast.regions.review import with_review_outcome


class SiteAttention(models.TextChoices):
    SETUP_INCOMPLETE = "setup_incomplete"
    REVIEW_REJECTED = "review_rejected"
    SUSPENDED = "suspended"
    CONNECTION_UNAVAILABLE = "connection_unavailable"


@dataclass(frozen=True)
class Site:
    region: Region
    plant: Plant | None

    @property
    def edit_blocked_reason(self) -> PlantEditBlock | None:
        return plant_edit_block(self.region)

    @property
    def attention(self) -> SiteAttention | None:
        """The one reason a customer acts on first, from stored rows only."""
        if self.plant is None:
            return SiteAttention.SETUP_INCOMPLETE
        if self.region.status == RegionStatus.REJECTED:
            return SiteAttention.REVIEW_REJECTED
        block = self.edit_blocked_reason
        return None if block is None else SiteAttention(block.value)


def site_queryset(tenant_id: int) -> QuerySet[Region]:
    return (
        with_review_outcome(Region.objects.active().filter(tenant_id=tenant_id))
        .select_related("approved_binding__provider_account")
        .order_by("id")
    )


def sites(regions: Sequence[Region]) -> list[Site]:
    plants: dict[int, Plant] = {}
    for plant in (
        Plant.objects.filter(region__in=regions)
        .select_related("current_configuration", "submission")
        .order_by("id")
    ):
        plants.setdefault(plant.region_id, plant)
    return [Site(region, plants.get(region.pk)) for region in regions]


def submit_site(
    actor: User,
    tenant_id: int,
    parameters: PlantParameters,
    *,
    latitude: float,
    longitude: float,
    timezone: str,
    platform: bool = False,
) -> Region:
    with transaction.atomic():
        region = submit_region(
            actor,
            tenant_id,
            name=parameters.name,
            latitude=latitude,
            longitude=longitude,
            timezone=timezone,
            platform=platform,
        )
        create_plant(actor, tenant_id, region.pk, parameters, platform=platform)
        return site_queryset(tenant_id).get(pk=region.pk)
