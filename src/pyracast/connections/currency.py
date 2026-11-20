from pyracast.connections.models import SiteBinding
from pyracast.regions.models import Region


def binding_is_current(binding: SiteBinding, loaded_region: Region | None = None) -> bool:
    current = {
        "pk": binding.region_id,
        "deleted_at": None,
        "status": "approved",
        "revision": binding.region_revision,
        "approved_binding_id": binding.pk,
    }
    region_is_current = (
        Region.objects.filter(**current).exists()
        if loaded_region is None
        else all(getattr(loaded_region, field) == value for field, value in current.items())
    )
    return (
        region_is_current
        and binding.provider_account.enabled
        and binding.provider_account.authorized_at is not None
        and binding.validated_credential_revision == binding.provider_account.credential_revision
    )
