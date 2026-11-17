import os
import sys
from typing import Any

import pika  # type: ignore[import-untyped]
import psycopg

from scripts import start_postgres, start_rabbitmq

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
REJECTED = "{service} rejected the configured credentials; check {settings} in .env."


def _postgres_available() -> bool:
    try:
        with psycopg.connect(
            host=os.environ.get("PGHOST", "127.0.0.1"),
            port=os.environ.get("PGPORT", "5432"),
            dbname=os.environ.get("PGDATABASE", "pyracast"),
            user=os.environ.get("PGUSER", "pyracast"),
            password=os.environ.get("PGPASSWORD", ""),
            connect_timeout=3,
            autocommit=True,
        ) as connection:
            connection.execute("SELECT 1")
    except (
        psycopg.errors.InvalidPassword,
        psycopg.errors.InvalidAuthorizationSpecification,
    ):
        raise RuntimeError(
            REJECTED.format(service="PostgreSQL", settings="PGUSER and PGPASSWORD")
        ) from None
    except psycopg.Error:
        return False
    return True


def _ensure_postgres() -> None:
    host = os.environ.get("PGHOST", "127.0.0.1")
    port = os.environ.get("PGPORT", "5432")
    if host not in LOCAL_HOSTS:
        raise RuntimeError(
            f"The launcher uses only a loopback database; PGHOST is {host}. "
            "Run remote databases without the launcher."
        )
    print(f"PostgreSQL {host}:{port}/{os.environ.get('PGDATABASE', 'pyracast')}", flush=True)
    if _postgres_available():
        return
    start_postgres.main([])
    if not _postgres_available():
        raise RuntimeError("PostgreSQL did not become available; see its log in the data root.")


def _rabbit_parameters() -> Any:
    try:
        parameters = pika.URLParameters(
            os.environ.get("PYRACAST_RABBITMQ_URL", "amqp://guest:guest@127.0.0.1:5672/%2F")
        )
    except ValueError, TypeError:
        raise RuntimeError("The configured RabbitMQ connection is invalid.") from None
    parameters.socket_timeout = 3
    parameters.stack_timeout = 5
    parameters.connection_attempts = 1
    parameters.blocked_connection_timeout = 3
    return parameters


def _rabbit_ready(parameters: Any) -> bool:
    try:
        connection = pika.BlockingConnection(parameters)
        connection.close()
    except pika.exceptions.ProbableAuthenticationError, pika.exceptions.ProbableAccessDeniedError:
        raise RuntimeError(
            REJECTED.format(service="RabbitMQ", settings="PYRACAST_RABBITMQ_URL")
        ) from None
    except pika.exceptions.AMQPError, OSError:
        return False
    return True


def _ensure_rabbitmq() -> None:
    parameters = _rabbit_parameters()
    print(f"RabbitMQ {parameters.host}:{parameters.port}", flush=True)
    if _rabbit_ready(parameters):
        return
    if sys.platform != "win32" or parameters.host not in LOCAL_HOSTS:
        raise RuntimeError("RabbitMQ is unavailable; start it with the README Docker command.")
    start_rabbitmq.main([])
    if not _rabbit_ready(parameters):
        raise RuntimeError("RabbitMQ did not become available; see broker.log in its data root.")


def ensure_dependencies() -> None:
    _ensure_postgres()
    _ensure_rabbitmq()
