from collections.abc import Sequence
from dataclasses import dataclass

from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import QuerySet

from pyracast.plants.models import Plant
from pyracast.plants.operations import create_plant
from pyracast.regions.models import Region
from pyracast.regions.operations import submit_region


@dataclass(frozen=True)
class Site:
    region: Region
    plant: Plant | None


def site_queryset(tenant_id: int) -> QuerySet[Region]:
    return Region.objects.active().filter(tenant_id=tenant_id).order_by("id")


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
    *,
    name: str,
    latitude: float,
    longitude: float,
    timezone: str,
    dc_capacity_mw: float,
    inverter_capacity_mw: float | None = None,
    export_limit_mw: float | None = None,
    measurement_basis: str = "net_export",
    mounting_type: str = "unknown",
    surface_tilt_degrees: float | None = None,
    surface_azimuth_degrees: float | None = None,
    platform: bool = False,
) -> Region:
    with transaction.atomic():
        region = submit_region(
            actor,
            tenant_id,
            name=name,
            latitude=latitude,
            longitude=longitude,
            timezone=timezone,
            platform=platform,
        )
        create_plant(
            actor,
            tenant_id,
            region.pk,
            name=name,
            dc_capacity_mw=dc_capacity_mw,
            inverter_capacity_mw=inverter_capacity_mw,
            export_limit_mw=export_limit_mw,
            measurement_basis=measurement_basis,
            mounting_type=mounting_type,
            surface_tilt_degrees=surface_tilt_degrees,
            surface_azimuth_degrees=surface_azimuth_degrees,
            platform=platform,
        )
        return site_queryset(tenant_id).get(pk=region.pk)
