from pyracast.connections.models import SiteBinding
from pyracast.regions.models import Region, RegionStatus
from tests.support.domain import System


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
