from datetime import timedelta
from typing import Any

import pytest
from django.db import IntegrityError, transaction

from pyracast.accounts.models import Tenant
from pyracast.connections.models import ProviderAccount, SiteBinding
from pyracast.forecasts.models import (
    CollectionJob,
    ForecastSnapshot,
    HistoricalReferenceImport,
    RequestAttempt,
)
from pyracast.regions.models import Region
from tests.support.domain import NOW, make_system

pytestmark = pytest.mark.django_db(transaction=True)


def test_approved_binding_cannot_cross_tenant_regions() -> None:
    system = make_system()
    other = Region.objects.create(
        tenant=Tenant.objects.create(name="Other"),
        name="Other site",
        latitude=40,
        longitude=29,
        timezone="Europe/Istanbul",
    )
    with pytest.raises(IntegrityError, match="approved_binding_region_fk"), transaction.atomic():
        Region.objects.filter(pk=other.pk).update(approved_binding=system.binding)
    Region.objects.filter(pk=system.region.pk).update(approved_binding=system.binding)
    with pytest.raises(IntegrityError, match="approved_binding_region_fk"), transaction.atomic():
        SiteBinding.objects.filter(pk=system.binding.pk).update(region=other)


@pytest.mark.parametrize(
    ("changes", "constraint"),
    [
        ({"capacity_kw": float("inf")}, "binding_capacity_finite"),
        ({"capacity_kw": float("nan")}, "binding_capacity_finite"),
        ({"latitude": -91}, "binding_latitude"),
        ({"latitude": float("nan")}, "binding_latitude"),
        ({"longitude": 181}, "binding_longitude"),
        ({"power_basis": "xx"}, "binding_power_basis"),
        ({"azimuth_degrees": 360}, "binding_azimuth"),
        ({"azimuth_degrees": float("nan")}, "binding_azimuth"),
        ({"losses_percent": 100}, "binding_losses"),
        ({"health": "other"}, "binding_health"),
    ],
)
def test_binding_rejects_invalid_physical_metadata(
    changes: dict[str, Any], constraint: str
) -> None:
    system = make_system()
    with pytest.raises(IntegrityError, match=constraint), transaction.atomic():
        SiteBinding.objects.filter(pk=system.binding.pk).update(**changes)


def test_request_account_and_job_must_match_the_binding() -> None:
    system = make_system(approved=True)
    job = CollectionJob.objects.get(binding=system.binding)
    account = ProviderAccount.objects.create(provider="fixture", external_account_id="other")
    other_binding = SiteBinding.objects.create(
        region=system.region,
        region_revision=system.region.revision + 1,
        provider_account=account,
        site_id="other-site",
        capacity_kw=1,
        latitude=40,
        longitude=29,
        timezone="Europe/Istanbul",
    )
    values = {"utc_day": NOW.date(), "created_at": NOW, "purpose": "forecast"}
    with pytest.raises(IntegrityError, match="request_binding_account_fk"), transaction.atomic():
        RequestAttempt.objects.create(provider_account=account, binding=system.binding, **values)
    with pytest.raises(IntegrityError, match="request_job_binding_fk"), transaction.atomic():
        RequestAttempt.objects.create(
            provider_account=account, binding=other_binding, job=job, **values
        )
    with pytest.raises(IntegrityError, match="request_job_requires_binding"), transaction.atomic():
        RequestAttempt.objects.create(provider_account=system.account, job=job, **values)
    attempt = RequestAttempt.objects.create(
        provider_account=system.account, binding=system.binding, job=job, **values
    )
    with pytest.raises(IntegrityError, match="request_attempt_outcome"), transaction.atomic():
        RequestAttempt.objects.filter(pk=attempt.pk).update(outcome="untracked")
    RequestAttempt.objects.create(provider_account=account, **values)
    with pytest.raises(IntegrityError, match="request_binding_account_fk"), transaction.atomic():
        SiteBinding.objects.filter(pk=system.binding.pk).update(provider_account=account)


def test_historical_job_cannot_use_an_import_from_another_binding() -> None:
    system = make_system(approved=True)
    other_binding = SiteBinding.objects.create(
        region=system.region,
        region_revision=system.region.revision + 1,
        provider_account=system.account,
        site_id="other-site",
        capacity_kw=1,
        latitude=40,
        longitude=29,
        timezone="Europe/Istanbul",
    )
    reference_import = HistoricalReferenceImport.objects.create(
        binding=other_binding,
        sha256="a" * 64,
        source_name="fixture",
        source_settings={},
        imported_by=system.admin,
        imported_at=NOW,
        interval_count=1,
    )
    job = CollectionJob.objects.get(binding=system.binding)
    imported = {"endpoint": "historical_import", "status": "succeeded"}
    with pytest.raises(IntegrityError, match="collection_import_binding_fk"), transaction.atomic():
        CollectionJob.objects.filter(pk=job.pk).update(
            reference_import=reference_import, **imported
        )
    CollectionJob.objects.filter(pk=job.pk).update(
        binding=other_binding, reference_import=reference_import, **imported
    )
    with pytest.raises(IntegrityError, match="are immutable"), transaction.atomic():
        HistoricalReferenceImport.objects.filter(pk=reference_import.pk).update(
            binding=system.binding
        )


@pytest.mark.parametrize(
    "bounds",
    [(NOW, None), (None, NOW), (NOW, NOW), (NOW, NOW - timedelta(seconds=1))],
)
def test_snapshot_requires_a_complete_increasing_coverage_pair(
    bounds: tuple[object, object],
) -> None:
    system = make_system(approved=True)
    job = CollectionJob.objects.get(binding=system.binding)
    stored: dict[str, Any] = {
        "job": job,
        "binding": system.binding,
        "raw_payload": {},
        "normalized_intervals": [],
        "collected_at": NOW,
        "source_kind": "forecast",
    }
    with pytest.raises(IntegrityError, match="snapshot_coverage_bounds"), transaction.atomic():
        ForecastSnapshot.objects.create(**stored, coverage_start=bounds[0], coverage_end=bounds[1])
    with pytest.raises(IntegrityError, match="snapshot_source_kind"), transaction.atomic():
        ForecastSnapshot.objects.create(**stored | {"source_kind": "other"})
    ForecastSnapshot.objects.create(
        **stored, coverage_start=NOW, coverage_end=NOW + timedelta(minutes=30)
    )
    with pytest.raises(IntegrityError, match="collection_job_status"), transaction.atomic():
        CollectionJob.objects.filter(pk=job.pk).update(status="lost")


@pytest.mark.parametrize("source_kind", [None, "unknown"])
def test_snapshot_names_whether_it_holds_a_forecast_or_estimated_actuals(
    source_kind: str | None,
) -> None:
    system = make_system(approved=True)
    job = CollectionJob.objects.get(binding=system.binding)
    values: dict[str, Any] = {} if source_kind is None else {"source_kind": source_kind}
    with pytest.raises(IntegrityError, match="snapshot_source_kind"), transaction.atomic():
        ForecastSnapshot.objects.create(
            job=job,
            binding=system.binding,
            raw_payload={},
            normalized_intervals=[],
            collected_at=NOW,
            **values,
        )
