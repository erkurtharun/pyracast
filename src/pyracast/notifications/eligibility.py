from pyracast.connections.models import SiteBinding
from pyracast.regions.models import Region


def binding_can_notify(binding: SiteBinding) -> bool:
    return (
        Region.objects.active()
        .filter(
            pk=binding.region_id,
            status="approved",
            revision=binding.region_revision,
            approved_binding_id=binding.pk,
            approved_binding__provider_account__enabled=True,
            approved_binding__provider_account__authorized_at__isnull=False,
        )
        .exists()
    )
