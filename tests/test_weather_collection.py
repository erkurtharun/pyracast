from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from django.conf import LazySettings

from pyracast.background import clock as monotonic_clock
from pyracast.calculation import calculate
from pyracast.connections.contracts import ProviderData
from pyracast.connections.models import SiteBinding
from pyracast.connections.secrets import encrypt_secret
from pyracast.forecasts import runtime
from pyracast.forecasts.jobs.claim import LEASE_DURATION
from pyracast.forecasts.models import (
    CollectionJob,
    ForecastResult,
    ForecastSnapshot,
    RequestAttempt,
    SnapshotWeatherError,
)
from pyracast.forecasts.sources import snapshot_data
from pyracast.orchestration.kinds import COLLECTION
from pyracast.providers import FixtureProvider
from pyracast.weather import OpenMeteoProvider
from pyracast.weather_data import WeatherData
from tests.support.domain import NOW, FakeAdapter, make_system
from tests.support.jobs import collect
from tests.support.weather_collection import WeatherFixture

pytestmark = pytest.mark.django_db(transaction=True)


def test_weather_is_archived_separately_without_extra_hobby_quota() -> None:
    make_system(approved=True)
    job = CollectionJob.objects.get()
    weather = WeatherFixture()
    assert collect(job.pk, FakeAdapter(), calculate, NOW, weather_provider=weather)
    snapshot = ForecastSnapshot.objects.get()
    assert snapshot.raw_payload == {"fixture": True}
    assert snapshot.weather_payload is not None
    assert snapshot.weather_payload["provider"] == "open_meteo"
    assert snapshot.weather_error == ""
    result = ForecastResult.objects.get().payload
    assert result["complete"] is False
    assert result["energy_kwh_m2"] is None
    assert result["weather_reference"]["energy_kwh_m2"] == pytest.approx(14.4)
    assert result["weather_reference"]["complete"] is True
    assert "snow_loss_unknown" in result["weather_reference"]["quality_flags"]
    assert weather.calls == 1
    assert RequestAttempt.objects.filter(job=job).count() == 1
    assert not collect(job.pk, FakeAdapter(), calculate, NOW, weather_provider=weather)
    assert weather.calls == 1


def test_weather_failure_does_not_prevent_hobby_publication() -> None:
    make_system(approved=True)
    weather = WeatherFixture(fail=True)
    assert collect(
        CollectionJob.objects.get().pk, FakeAdapter(), calculate, NOW, weather_provider=weather
    )
    result = ForecastResult.objects.get().payload
    assert result["weather_reference"]["status"] == "unavailable"
    assert result["weather_reference"]["attribution"]["license"] == "CC BY 4.0"
    assert ForecastSnapshot.objects.get().weather_payload is None


def test_oversized_json_integer_does_not_prevent_hobby_publication() -> None:
    make_system(approved=True)
    weather = OpenMeteoProvider(
        httpx.MockTransport(lambda request: httpx.Response(200, content=b"9" * 5000))
    )
    assert collect(
        CollectionJob.objects.get().pk, FakeAdapter(), calculate, NOW, weather_provider=weather
    )
    assert ForecastSnapshot.objects.get().raw_payload == {"fixture": True}
    assert ForecastResult.objects.get().payload["weather_reference"]["status"] == "unavailable"


@pytest.mark.parametrize("interruption", ["expired", "reclaimed"])
def test_interrupted_weather_preserves_hobby_acquisition_and_rejects_stale_attachment(
    monkeypatch: pytest.MonkeyPatch, interruption: str
) -> None:
    make_system(approved=True)
    job = CollectionJob.objects.get()
    weather = WeatherFixture()
    original = weather.fetch
    elapsed = 0.0
    monkeypatch.setattr(monotonic_clock, "time", SimpleNamespace(monotonic=lambda: elapsed))
    recovery_at = NOW + LEASE_DURATION + timedelta(seconds=1)

    def fetch(latitude: float, longitude: float, start: datetime, end: datetime) -> WeatherData:
        nonlocal elapsed
        assert ForecastSnapshot.objects.get().raw_payload == {"fixture": True}
        result = original(latitude, longitude, start, end)
        elapsed = LEASE_DURATION.total_seconds() + 1
        if interruption == "reclaimed":
            assert collect(job.pk, FakeAdapter(), calculate, recovery_at, weather_provider=weather)
        return result

    monkeypatch.setattr(weather, "fetch", fetch)
    assert not collect(job.pk, FakeAdapter(), calculate, NOW, weather_provider=weather)
    snapshot = ForecastSnapshot.objects.get()
    assert snapshot.weather_payload is None
    assert snapshot.weather_error == "weather_acquisition_interrupted"
    if interruption != "reclaimed":
        assert collect(job.pk, FakeAdapter(), calculate, recovery_at, weather_provider=weather)
    assert weather.calls == 1
    assert RequestAttempt.objects.filter(job=job).count() == 1
    assert ForecastResult.objects.get().payload["weather_reference"]["status"] == "unavailable"
    snapshot.refresh_from_db()
    assert snapshot.weather_payload is None


def test_retry_reuses_both_pinned_acquisitions() -> None:
    make_system(approved=True)
    job = CollectionJob.objects.get()
    weather = WeatherFixture()

    def fail(binding: SiteBinding, day: object, data: ProviderData) -> dict[str, object]:
        raise ValueError("retry calculation")

    assert not collect(job.pk, FakeAdapter(), fail, NOW, weather_provider=weather)
    snapshot = ForecastSnapshot.objects.get()
    assert snapshot.weather_payload is not None
    saved = snapshot.weather_payload.copy()
    assert collect(
        job.pk, FakeAdapter(), calculate, NOW + timedelta(minutes=2), weather_provider=weather
    )
    snapshot.refresh_from_db()
    assert snapshot.weather_payload == saved
    assert weather.calls == 1
    assert RequestAttempt.objects.filter(job=job).count() == 1
    result = cast(dict[str, Any], ForecastResult.objects.get().payload["weather_reference"])
    assert result["collected_at"] == NOW.astimezone(UTC).isoformat()


def test_weather_window_uses_native_forecast_coverage() -> None:
    make_system(approved=True)
    weather = WeatherFixture()
    assert collect(
        CollectionJob.objects.get().pk, FixtureProvider(), calculate, NOW, weather_provider=weather
    )
    snapshot = ForecastSnapshot.objects.get()
    assert snapshot.weather_payload is not None
    assert snapshot.coverage_start is not None
    assert snapshot.coverage_end is not None
    assert snapshot.weather_payload["requested_start"] == snapshot.coverage_start.isoformat()
    assert snapshot.weather_payload["requested_end"] == snapshot.coverage_end.isoformat()


@pytest.mark.parametrize("payload", [{"provider": "invalid"}, [], "invalid"])
def test_invalid_saved_weather_does_not_remove_the_primary_source(payload: object) -> None:
    source = ForecastSnapshot(
        raw_payload={"original": True},
        normalized_intervals=[],
        source_kind="forecast",
        collected_at=NOW,
        weather_payload=payload,
    )
    data = snapshot_data(source)
    assert data.raw_payload == {"original": True}
    assert data.weather is None
    assert data.weather_error is SnapshotWeatherError.INVALID_SCHEMA


def test_a_stored_weather_error_is_read_back_as_its_member() -> None:
    source = ForecastSnapshot(
        raw_payload={},
        normalized_intervals=[],
        source_kind="forecast",
        collected_at=NOW,
        weather_error="weather_request_failed",
    )
    assert snapshot_data(source).weather_error is SnapshotWeatherError.REQUEST_FAILED


class HistoricalFixture(FakeAdapter):
    def fetch_history(self, binding: SiteBinding, target_date: object, days: int) -> ProviderData:
        return ProviderData({}, (), collected_at=NOW, source_kind="estimated_actuals")


@pytest.mark.parametrize(
    ("enabled", "endpoint", "has_weather"),
    [(True, "forecasts", True), (False, "forecasts", False), (True, "estimated_actuals", False)],
)
def test_runtime_only_enriches_enabled_hobby_forecasts(
    monkeypatch: pytest.MonkeyPatch,
    settings: LazySettings,
    enabled: bool,
    endpoint: str,
    has_weather: bool,
) -> None:
    system = make_system(approved=True)
    system.account.provider = "solcast"
    system.account.encrypted_api_key = encrypt_secret("test-only-solcast-key")
    system.account.save(update_fields=("provider", "encrypted_api_key"))
    job = CollectionJob.objects.get()
    job.endpoint = endpoint
    job.save(update_fields=("endpoint",))
    settings.PYRACAST_WEATHER_ENABLED = enabled
    settings.PYRACAST_OPEN_METEO_API_KEY = "artificial-weather-key"
    monkeypatch.setattr(runtime, "datetime", SimpleNamespace(now=lambda _: NOW))
    monkeypatch.setattr(runtime, "adapter_for", lambda binding: HistoricalFixture())
    weather = WeatherFixture()

    def provider(*, api_key: str | None) -> OpenMeteoProvider:
        assert api_key == "artificial-weather-key"
        return weather

    monkeypatch.setattr(runtime, "OpenMeteoProvider", provider)
    COLLECTION.deliver(job.pk)
    snapshot = ForecastSnapshot.objects.get()
    assert (snapshot.weather_payload is not None) is has_weather
    assert weather.calls == int(has_weather)
