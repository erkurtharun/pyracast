import os
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import dramatiq
from django.conf import settings
from django.db import close_old_connections, connections
from dramatiq import Actor, Broker
from dramatiq.brokers.rabbitmq import RabbitmqBroker

from pyracast.calculation import calculate
from pyracast.forecasts.jobs import collect, pending_job_ids
from pyracast.forecasts.models import CollectionJob
from pyracast.forecasts.scheduler import enqueue_due
from pyracast.providers import adapter_for
from pyracast.weather import OpenMeteoProvider


def deliver(job_id: int) -> None:
    close_old_connections()
    try:
        job = CollectionJob.objects.select_related("binding__provider_account").get(pk=job_id)
        weather = (
            OpenMeteoProvider(api_key=os.environ.get("PYRACAST_OPEN_METEO_API_KEY") or None)
            if settings.PYRACAST_WEATHER_ENABLED
            and job.binding.provider_account.provider == "solcast"
            and job.endpoint == "forecasts"
            else None
        )
        collect(
            job_id, adapter_for(job.binding), calculate, datetime.now(UTC), weather_provider=weather
        )
    finally:
        connections.close_all()


def make_actor(broker: Broker) -> Actor[[int], None]:
    return dramatiq.actor(broker=broker, actor_name="collect_forecast", max_retries=0)(deliver)


def make_broker() -> RabbitmqBroker:
    configured = os.environ.get("PYRACAST_RABBITMQ_URL", "amqp://guest:guest@127.0.0.1:5672/%2F")
    urls = []
    for url in configured.split(";"):
        parts = urlsplit(url)
        parameters = {
            "socket_timeout": "5",
            "stack_timeout": "10",
            "blocked_connection_timeout": "10",
            "heartbeat": "30",
            "connection_attempts": "1",
            **dict(parse_qsl(parts.query, keep_blank_values=True)),
        }
        urls.append(urlunsplit(parts._replace(query=urlencode(parameters))))
    return RabbitmqBroker(url=";".join(urls), confirm_delivery=True)


def dispatch(actor: Actor[[int], None]) -> int:
    now = datetime.now(UTC)
    enqueue_due(now)
    job_ids = pending_job_ids(now)
    for job_id in job_ids:
        actor.send(job_id)
    return len(job_ids)
