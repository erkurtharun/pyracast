from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Prefetch, QuerySet

from pyracast.accounts.operations import require_member
from pyracast.plants.models import Plant
from pyracast.plants.operations import create_plant
from pyracast.regions.models import Region
from pyracast.regions.operations import submit_region


def site_queryset(actor: User, tenant_id: int) -> QuerySet[Region]:
    require_member(actor, tenant_id)
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
) -> Region:
    with transaction.atomic():
        region = submit_region(
            actor,
            tenant_id,
            name=name,
            latitude=latitude,
            longitude=longitude,
            timezone=timezone,
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
        )
        return site_queryset(actor, tenant_id).get(pk=region.pk)
