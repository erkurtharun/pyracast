from collections.abc import Iterable

from django.db.models import F, Q

from pyracast.connections.models import SiteBinding
from pyracast.regions.models import Region, RegionStatus


def _notifiable() -> Q:
    return Q(
        status=RegionStatus.APPROVED,
        revision=F("approved_binding__region_revision"),
        approved_binding__provider_account__enabled=True,
        approved_binding__provider_account__authorized_at__isnull=False,
    )


def binding_can_notify(binding: SiteBinding) -> bool:
    return (
        Region.objects.active()
        .filter(_notifiable(), pk=binding.region_id, approved_binding_id=binding.pk)
        .exists()
    )


def notifiable_regions(region_ids: Iterable[int]) -> set[int]:
    return set(
        Region.objects.active()
        .filter(_notifiable(), pk__in=list(region_ids))
        .values_list("pk", flat=True)
    )
