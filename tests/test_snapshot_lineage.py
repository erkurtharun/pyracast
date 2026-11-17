from datetime import timedelta
from typing import Any

import pytest
from django.db import IntegrityError, transaction

from pyracast.connections.models import SiteBinding
from pyracast.forecasts.models import (
    CollectionJob,
    ForecastSnapshot,
    ReferenceCapacityNormalization,
)
from tests.support.domain import NOW, make_system

pytestmark = pytest.mark.django_db(transaction=True)


def test_snapshot_and_normalization_share_the_target_job_binding() -> None:
    system = make_system(approved=True)
    source_job = CollectionJob.objects.get(binding=system.binding)
    source = ForecastSnapshot.objects.create(
        job=source_job,
        binding=system.binding,
        raw_payload={},
        normalized_intervals=[],
        collected_at=NOW,
        source_kind="forecast",
    )
    target = SiteBinding.objects.get(pk=system.binding.pk)
    target.pk = None
    target.region_revision += 1
    target.capacity_kw = 2
    target.save()
    job = CollectionJob.objects.create(
        binding=target,
        endpoint="scaled_forecasts",
        scheduled_slot=NOW,
        target_date=NOW.date(),
        available_at=NOW,
    )
    with pytest.raises(IntegrityError, match="snapshot_job_binding_fk"), transaction.atomic():
        ForecastSnapshot.objects.create(
            job=job,
            binding=system.binding,
            raw_payload={},
            normalized_intervals=[],
            collected_at=NOW,
            source_kind="forecast",
        )
    snapshot = ForecastSnapshot.objects.create(
        job=job,
        binding=target,
        raw_payload={},
        normalized_intervals=[],
        collected_at=NOW,
        source_kind="forecast",
    )
    values = dict(
        source_snapshot=source,
        snapshot=snapshot,
        source_capacity_kw=1,
        target_capacity_kw=2,
        factor=2,
        authorized_by=system.admin,
        authorization_reference="Authorized fixture conversion",
        created_at=NOW,
    )
    with (
        pytest.raises(IntegrityError, match="normalization_snapshot_binding_fk"),
        transaction.atomic(),
    ):
        ReferenceCapacityNormalization.objects.create(target_binding=system.binding, **values)
    record = ReferenceCapacityNormalization.objects.create(target_binding=target, **values)
    with pytest.raises(IntegrityError, match="normalization_distinct_source"), transaction.atomic():
        ReferenceCapacityNormalization.objects.filter(pk=record.pk).update(source_snapshot=snapshot)
    with pytest.raises(IntegrityError, match="snapshot_job_binding_fk"), transaction.atomic():
        CollectionJob.objects.filter(pk=job.pk).update(binding=system.binding)
    other_job = CollectionJob.objects.create(
        binding=target,
        endpoint="scaled_forecasts",
        scheduled_slot=NOW + timedelta(minutes=1),
        target_date=NOW.date(),
        available_at=NOW,
    )
    other_snapshot = ForecastSnapshot.objects.create(
        job=other_job,
        binding=target,
        raw_payload={},
        normalized_intervals=[],
        collected_at=NOW,
        source_kind="forecast",
    )
    values["snapshot"] = other_snapshot
    with (
        pytest.raises(IntegrityError, match="unique_reference_capacity_normalization"),
        transaction.atomic(),
    ):
        ReferenceCapacityNormalization.objects.create(target_binding=target, **values)


@pytest.mark.parametrize(
    ("field", "value", "constraint"),
    [
        ("source_capacity_kw", 0, "normalization_source_capacity"),
        ("source_capacity_kw", -1, "normalization_source_capacity"),
        ("source_capacity_kw", float("inf"), "normalization_source_capacity"),
        ("source_capacity_kw", float("nan"), "normalization_source_capacity"),
        ("target_capacity_kw", 0, "normalization_target_capacity"),
        ("target_capacity_kw", float("inf"), "normalization_target_capacity"),
        ("target_capacity_kw", float("nan"), "normalization_target_capacity"),
    ],
)
def test_normalization_preserves_finite_positive_capacity_provenance(
    field: str, value: float, constraint: str
) -> None:
    system = make_system(approved=True)
    source_job = CollectionJob.objects.get(binding=system.binding)
    source = ForecastSnapshot.objects.create(
        job=source_job,
        binding=system.binding,
        raw_payload={},
        normalized_intervals=[],
        collected_at=NOW,
        source_kind="forecast",
    )
    job = CollectionJob.objects.create(
        binding=system.binding,
        endpoint="scaled_forecasts",
        scheduled_slot=NOW,
        target_date=NOW.date(),
        available_at=NOW,
    )
    snapshot = ForecastSnapshot.objects.create(
        job=job,
        binding=system.binding,
        raw_payload={},
        normalized_intervals=[],
        collected_at=NOW,
        source_kind="forecast",
    )
    values: dict[str, Any] = dict(
        source_snapshot=source,
        snapshot=snapshot,
        target_binding=system.binding,
        source_capacity_kw=1,
        target_capacity_kw=2,
        factor=2,
        authorized_by=system.admin,
        authorization_reference="Authorized fixture conversion",
        created_at=NOW,
    )
    values[field] = value
    with pytest.raises(IntegrityError, match=constraint), transaction.atomic():
        ReferenceCapacityNormalization.objects.create(**values)
