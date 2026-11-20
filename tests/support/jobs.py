from datetime import datetime

from django.db import connection

from pyracast.connections.contracts import Calculator, ProviderAdapter, ProviderData
from pyracast.connections.models import SiteBinding
from pyracast.forecasts.jobs import collect as collection
from pyracast.forecasts.jobs import publish as publication
from pyracast.forecasts.jobs.claim import Lease
from pyracast.forecasts.models import ForecastSnapshot
from pyracast.plants.publication import PUBLICATION, Prepared
from pyracast.weather import OpenMeteoProvider


def calculate(binding: SiteBinding, target_date: object, data: ProviderData) -> dict[str, object]:
    assert not connection.in_atomic_block
    assert data.collected_at is not None
    return {"energy_kwh_m2": None, "complete": False, "flags": ["incomplete_day"]}


def collect(
    job_id: int,
    adapter: ProviderAdapter,
    calculator: Calculator,
    now: datetime,
    *,
    weather_provider: OpenMeteoProvider | None = None,
) -> bool:
    """Collects with the plant publication the collection actor uses."""
    return collection.collect(
        job_id, adapter, calculator, now, hooks=PUBLICATION, weather_provider=weather_provider
    )


def publish(
    lease: Lease,
    snapshot: ForecastSnapshot,
    payload: dict[str, object],
    now: datetime,
    *,
    daily_payloads: dict[str, dict[str, object]] | None = None,
    prepared_issuances: Prepared | None = None,
) -> bool:
    return publication.publish(
        lease,
        snapshot,
        payload,
        now,
        hooks=PUBLICATION,
        daily_payloads=daily_payloads,
        prepared_issuances=prepared_issuances,
    )
