from django.conf import settings

from pyracast.regions.models import Region


def workspace_url(tenant_id: int, path: str = "") -> str:
    """Links in mail start at the configured public origin, never at a request's host."""
    return f"{settings.PYRACAST_PUBLIC_ORIGIN}/app/w/{tenant_id}/{path}"


def plant_url(region: Region) -> str:
    return workspace_url(region.tenant_id, f"plants/{region.pk}/forecast/")
