import logging
from datetime import UTC, datetime

import dramatiq
from django.conf import settings
from django.db import close_old_connections, connections
from dramatiq import Actor, Broker

from pyracast.background.failures import PhaseOutcome, log_failure
from pyracast.calculation import calculate
from pyracast.forecasts.jobs import collect, pending_job_ids
from pyracast.forecasts.models import CollectionEndpoint, CollectionJob
from pyracast.forecasts.scheduler import CollectionSchedulingError, enqueue_due
from pyracast.providers import adapter_for
from pyracast.weather import OpenMeteoProvider

logger = logging.getLogger(__name__)


def deliver(job_id: int) -> None:
    close_old_connections()
    try:
        job = (
            CollectionJob.objects.select_related("binding__provider_account")
            .filter(pk=job_id)
            .first()
        )
        if job is None:
            return
        weather = (
            OpenMeteoProvider(api_key=settings.PYRACAST_OPEN_METEO_API_KEY or None)
            if settings.PYRACAST_WEATHER_ENABLED
            and job.binding.provider_account.provider == "solcast"
            and job.endpoint == CollectionEndpoint.FORECASTS
            else None
        )
        collect(
            job_id, adapter_for(job.binding), calculate, datetime.now(UTC), weather_provider=weather
        )
    finally:
        connections.close_all()


def make_actor(broker: Broker) -> Actor[[int], None]:
    return dramatiq.actor(broker=broker, actor_name="collect_forecast", max_retries=0)(deliver)


def dispatch_collections(actor: Actor[[int], None]) -> PhaseOutcome:
    now = datetime.now(UTC)
    error = ""
    try:
        enqueue_due(now)
    except CollectionSchedulingError as exc:
        error = f"collection_regions_failed:{len(exc.failed_region_ids)}"
        close_old_connections()
    except Exception as exc:
        log_failure(logger, "collection_scheduling_failed", exc)
        error = type(exc).__name__
        close_old_connections()
    published = 0
    try:
        for job_id in pending_job_ids(now):
            actor.send(job_id)
            published += 1
    except Exception as exc:
        log_failure(logger, "collection_publish_failed", exc, published=published)
        error = error or type(exc).__name__
    return PhaseOutcome(published=published, error=error)
