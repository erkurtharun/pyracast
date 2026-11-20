import functools
from collections.abc import Callable

import dramatiq
from django.db import close_old_connections, connections
from dramatiq import Actor, Broker

from pyracast.background.deadlines import time_limit_ms


def fresh_connections[**P](run: Callable[P, object]) -> Callable[P, None]:
    """A job starts without a stale database connection and leaves none to the next job."""

    @functools.wraps(run)
    def deliver(*args: P.args, **kwargs: P.kwargs) -> None:
        close_old_connections()
        try:
            run(*args, **kwargs)
        finally:
            connections.close_all()

    return deliver


def job_actor(
    broker: Broker, name: str, queue: str, deliver: Callable[[int], None]
) -> Actor[[int], None]:
    """The job's owner retries it, so Dramatiq never does; the ladder bounds how long it runs."""
    return dramatiq.actor(
        broker=broker,
        actor_name=name,
        queue_name=queue,
        max_retries=0,
        time_limit=time_limit_ms(name),
    )(deliver)
