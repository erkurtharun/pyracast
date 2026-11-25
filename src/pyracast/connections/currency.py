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
    return region_is_current and connection_is_authorized(binding)


def connection_is_authorized(binding: SiteBinding) -> bool:
    account = binding.provider_account
    return (
        account.enabled
        and account.authorized_at is not None
        and binding.validated_credential_revision == account.credential_revision
    )
