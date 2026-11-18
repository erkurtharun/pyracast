from datetime import timedelta

from pyracast.connections.models import SiteBinding
from pyracast.plants.models import Plant, PlantConfiguration
from pyracast.plants.operations import create_plant
from pyracast.regions.models import Region, RegionStatus
from tests.support.domain import NOW, System


def sibling(system: System) -> tuple[Region, SiteBinding]:
    region = Region.objects.create(
        tenant=system.tenant,
        name="Other region",
        latitude=41 + Region.objects.filter(tenant=system.tenant).count(),
        longitude=29,
        timezone=system.region.timezone,
    )
    binding = SiteBinding.objects.get(pk=system.binding.pk)
    binding.pk = None
    binding.region = region
    binding.latitude = region.latitude
    binding.longitude = region.longitude
    binding.timezone = region.timezone
    binding.save()
    region.status = RegionStatus.APPROVED
    region.approved_binding = binding
    region.save()
    return region, binding


def sibling_plant(system: System) -> Plant:
    region, _ = sibling(system)
    plant = create_plant(
        system.member, system.tenant.pk, region.pk, name="Second plant", dc_capacity_mw=2
    )
    assert plant.current_configuration_id is not None
    PlantConfiguration.objects.filter(pk=plant.current_configuration_id).update(
        created_at=NOW - timedelta(days=100)
    )
    return Plant.objects.get(pk=plant.pk)
