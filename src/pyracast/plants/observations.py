from datetime import datetime

from django.db.models import Exists, OuterRef, QuerySet

from pyracast.plants.models import ProductionObservation


def observation_heads(
    *, cutoff: datetime, observation_id_cutoff: int | None = None
) -> QuerySet[ProductionObservation]:
    eligible = ProductionObservation.objects.filter(recorded_at__lte=cutoff)
    if observation_id_cutoff is not None:
        eligible = eligible.filter(pk__lte=observation_id_cutoff)
    # Current-head metadata enforces live uniqueness; historical replay uses only
    # revisions available at the pinned timestamp and ID watermark.
    return eligible.filter(~Exists(eligible.filter(supersedes_id=OuterRef("pk"))))
