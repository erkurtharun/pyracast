from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Prefetch, QuerySet

from pyracast.accounts.authorization import tenant_membership
from pyracast.plants.models import Plant
from pyracast.plants.operations import create_plant
from pyracast.regions.models import Region
from pyracast.regions.operations import submit_region


def site_queryset(actor: User, tenant_id: int) -> QuerySet[Region]:
    tenant_membership(actor, tenant_id)
    return tenant_sites(tenant_id)


def tenant_sites(tenant_id: int) -> QuerySet[Region]:
    return (
        Region.objects.filter(tenant_id=tenant_id, deleted_at__isnull=True)
        .prefetch_related(
            Prefetch(
                "plant_set",
                queryset=Plant.objects.select_related("current_configuration", "submission"),
                to_attr="active_plants",
            )
        )
        .order_by("id")
    )


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
    surface_tilt: float | None = None,
    surface_azimuth: float | None = None,
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
            surface_tilt=surface_tilt,
            surface_azimuth=surface_azimuth,
            platform=platform,
        )
        return tenant_sites(tenant_id).get(pk=region.pk)
