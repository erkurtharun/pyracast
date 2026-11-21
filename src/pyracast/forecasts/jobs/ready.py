from datetime import datetime

from django.db.models import Q

from pyracast.background.lease import claimable
from pyracast.forecasts.models import CollectionJob, CollectionJobError, CollectionJobStatus
from pyracast.regions.models import active_region


def pending_job_ids(now: datetime, limit: int = 100) -> list[int]:
    return list(
        CollectionJob.objects.filter(
            active_region("binding__region__"),
            Q(available_at__lte=now)
            | Q(
                status=CollectionJobStatus.RETRY,
                last_error=CollectionJobError.PROVIDER_THROTTLED,
                binding__provider_account__request_blocked_until__isnull=True,
            ),
        )
        .filter(
            Q(status__in=(CollectionJobStatus.PENDING, CollectionJobStatus.RETRY))
            | claimable(CollectionJobStatus.RUNNING, now)
        )
        .order_by("available_at", "pk")
        .values_list("pk", flat=True)[:limit]
    )
