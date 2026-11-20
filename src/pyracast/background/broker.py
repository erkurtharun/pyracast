import os
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from dramatiq.brokers.rabbitmq import RabbitmqBroker


def make_broker() -> RabbitmqBroker:
    urls = []
    configured = os.environ.get("PYRACAST_RABBITMQ_URL", "amqp://guest:guest@127.0.0.1:5672/%2F")
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
