import os
import sys
import time
from typing import Any

import pika  # type: ignore[import-untyped]
import psycopg

from scripts import start_postgres, start_rabbitmq

LOCAL_HOSTS = {"127.0.0.1", "localhost"}
REJECTED = "{service} rejected the configured credentials; check {settings} in .env."


def _postgres_available(host: str, port: str) -> bool:
    try:
        with psycopg.connect(
            host=host,
            port=port,
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
    port = str(int(os.environ.get("PGPORT", "5432")))
    if host not in LOCAL_HOSTS:
        raise RuntimeError(
            f"The launcher uses only a loopback database on 127.0.0.1 or localhost; PGHOST is "
            f"{host}. Run other databases without the launcher."
        )
    print(f"PostgreSQL {host}:{port}/{os.environ.get('PGDATABASE', 'pyracast')}", flush=True)
    if _postgres_available(host, port):
        return
    start_postgres.main(["--port", port])
    if not _postgres_available(host, port):
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
    if sys.platform == "win32":
        if parameters.host not in LOCAL_HOSTS or parameters.port != start_rabbitmq.PORT:
            raise RuntimeError(
                f"RabbitMQ is unavailable at {parameters.host}:{parameters.port}; "
                f"the helper starts a broker only on 127.0.0.1:{start_rabbitmq.PORT}."
            )
        credentials = parameters.credentials
        supported = pika.URLParameters(start_rabbitmq.QUEUE_URL)
        if (
            not isinstance(credentials, pika.PlainCredentials)
            or credentials.username != supported.credentials.username
            or credentials.password != supported.credentials.password
            or parameters.virtual_host != supported.virtual_host
            or parameters.ssl_options is not None
        ):
            raise RuntimeError(
                "Automatic RabbitMQ provisioning supports only guest credentials, "
                "the / virtual host and plain AMQP. Start the configured broker separately."
            )
        start_rabbitmq.main([])
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if _rabbit_ready(parameters):
                return
            time.sleep(1)
        raise RuntimeError("RabbitMQ did not become available; see broker.log in its data root.")
    else:
        raise RuntimeError("RabbitMQ is unavailable; start it with the README Docker command.")


def ensure_dependencies() -> None:
    _ensure_postgres()
    _ensure_rabbitmq()
