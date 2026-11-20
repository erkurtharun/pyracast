from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from django.conf import settings
from django.core.checks import CheckMessage, Error
from django.core.exceptions import ImproperlyConfigured
from dramatiq.brokers.rabbitmq import RabbitmqBroker

from pyracast.background.deadlines import (
    BROKER_CALL_SECONDS,
    BROKER_HEARTBEAT_SECONDS,
    BROKER_SOCKET_SECONDS,
)

LOCAL_BROKER_URL = "amqp://guest:guest@127.0.0.1:5672/%2F"
MISSING_URL = "Set PYRACAST_RABBITMQ_URL; only development falls back to a local broker."


def broker_url() -> str:
    return settings.PYRACAST_RABBITMQ_URL.strip() or (LOCAL_BROKER_URL if settings.DEBUG else "")


def check_broker_url(**_kwargs: Any) -> list[CheckMessage]:
    return [] if broker_url() else [Error(MISSING_URL, id="background.E001")]


def _bounded(url: str) -> str:
    parts = urlsplit(url)
    parameters = {
        "socket_timeout": str(BROKER_SOCKET_SECONDS),
        "stack_timeout": str(BROKER_CALL_SECONDS),
        "blocked_connection_timeout": str(BROKER_CALL_SECONDS),
        "heartbeat": str(BROKER_HEARTBEAT_SECONDS),
        "connection_attempts": "1",
        **dict(parse_qsl(parts.query, keep_blank_values=True)),
    }
    return urlunsplit(parts._replace(query=urlencode(parameters)))


def make_broker() -> RabbitmqBroker:
    configured = broker_url()
    if not configured:
        raise ImproperlyConfigured(MISSING_URL)
    # Semicolons separate failover nodes.
    urls = ";".join(_bounded(url) for url in configured.split(";"))
    return RabbitmqBroker(url=urls, confirm_delivery=True)
