from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from runpy import run_path

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from pyracast.connections.models import SiteBinding
from pyracast.forecasts.models import CollectionJob
from pyracast.forecasts.scheduler import enqueue_due
from pyracast.regions.models import Region
from tests.support.domain import NOW, make_system


@pytest.mark.parametrize("value", ["1:30", "24:00", "16:60", "16:00:00", ""])
def test_invalid_cutoff_environment_is_rejected(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("PYRACAST_CUTOFF_LOCAL", value)
    with pytest.raises(ImproperlyConfigured, match="PYRACAST_CUTOFF_LOCAL"):
        run_path(str(Path("src/pyracast/settings.py")))


def test_cutoff_environment_parses_minutes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYRACAST_CUTOFF_LOCAL", "07:45")
    assert run_path(str(Path("src/pyracast/settings.py")))["PYRACAST_CUTOFF_LOCAL"] == time(7, 45)


@pytest.mark.django_db(transaction=True)
@override_settings(PYRACAST_CUTOFF_LOCAL=time(16, 30))
def test_configured_minute_sets_local_slot_and_deduplicates() -> None:
    make_system(approved=True)
    assert enqueue_due(NOW + timedelta(minutes=29)) == []
    ids = enqueue_due(NOW + timedelta(minutes=30))
    assert len(ids) == 2
    job = CollectionJob.objects.get(pk=ids[0])
    assert job.endpoint == "forecasts"
    assert (
        CollectionJob.objects.get(endpoint="estimated_actuals").scheduled_for == job.scheduled_for
    )
    assert job.scheduled_for == NOW + timedelta(minutes=30)
    assert job.target_date.isoformat() == "2026-11-10"
    assert enqueue_due(NOW + timedelta(hours=1)) == ids[:1]


@pytest.mark.django_db(transaction=True)
@override_settings(PYRACAST_CUTOFF_LOCAL=time(2, 30))
def test_spring_gap_skips_nonexistent_forecast_cutoff_but_collects_night_history() -> None:
    system = make_system(approved=True)
    Region.objects.filter(pk=system.region.pk).update(timezone="Europe/Berlin")
    SiteBinding.objects.filter(pk=system.binding.pk).update(
        validated_at=datetime(2026, 3, 28, 12, tzinfo=UTC)
    )
    CollectionJob.objects.filter(binding=system.binding).update(
        scheduled_for=datetime(2026, 3, 28, 12, tzinfo=UTC)
    )
    ids = enqueue_due(datetime(2026, 3, 29, 1, 0, tzinfo=UTC))
    assert len(ids) == 1
    assert CollectionJob.objects.get(pk=ids[0]).endpoint == "estimated_actuals"
    assert enqueue_due(datetime(2026, 3, 29, 1, 30, tzinfo=UTC)) == ids


@pytest.mark.django_db(transaction=True)
@override_settings(PYRACAST_CUTOFF_LOCAL=time(2, 30))
def test_fall_fold_uses_first_cutoff_occurrence() -> None:
    system = make_system(approved=True)
    Region.objects.filter(pk=system.region.pk).update(timezone="Europe/Berlin")
    SiteBinding.objects.filter(pk=system.binding.pk).update(
        validated_at=datetime(2026, 10, 24, 12, tzinfo=UTC)
    )
    CollectionJob.objects.filter(binding=system.binding).update(
        scheduled_for=datetime(2026, 10, 24, 12, tzinfo=UTC)
    )
    ids = enqueue_due(datetime(2026, 10, 25, 1, 15, tzinfo=UTC))
    assert len(ids) == 2
    assert CollectionJob.objects.get(pk__in=ids, endpoint="forecasts").scheduled_for == datetime(
        2026, 10, 25, 0, 30, tzinfo=UTC
    )
    history = CollectionJob.objects.get(pk__in=ids, endpoint="estimated_actuals")
    assert history.scheduled_for == datetime(2026, 10, 24, 22, tzinfo=UTC)
    assert history.target_date.isoformat() == "2026-10-24"
