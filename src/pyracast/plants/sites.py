from collections.abc import Sequence
from dataclasses import dataclass

from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import QuerySet

from pyracast.plants.models import Plant
from pyracast.plants.operations import create_plant
from pyracast.plants.parameters import PlantParameters
from pyracast.regions.models import Region
from pyracast.regions.operations import submit_region
from pyracast.regions.review import with_review_outcome


@dataclass(frozen=True)
class Site:
    region: Region
    plant: Plant | None


def site_queryset(tenant_id: int) -> QuerySet[Region]:
    return with_review_outcome(Region.objects.active().filter(tenant_id=tenant_id)).order_by("id")


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
