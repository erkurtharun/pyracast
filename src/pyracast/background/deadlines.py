import math
from dataclasses import dataclass
from datetime import timedelta

HTTP_CONNECT_SECONDS = 5
HTTP_READ_SECONDS = 20
# A whole answer, checked after each socket read: httpx bounds only the reads.
HTTP_BODY_SECONDS = 30
MAIL_SOCKET_SECONDS = 15
BROKER_SOCKET_SECONDS = 5
BROKER_CALL_SECONDS = 10
BROKER_HEARTBEAT_SECONDS = 30
DATABASE_CONNECT_SECONDS = 5
DATABASE_LOCK_SECONDS = 5
DATABASE_STATEMENT_SECONDS = 60
# Longer than any pause between two statements of one request or job transaction.
DATABASE_IDLE_IN_TRANSACTION_SECONDS = 120

LEASE = timedelta(minutes=5)
# Dramatiq interrupts a thread only between bytecodes: after its limit the actor can still wait
# one blocking call, at most a statement, before it stops. It must be gone before its lease ends.
LEASE_MARGIN = timedelta(seconds=DATABASE_STATEMENT_SECONDS + 30)
TIME_LIMIT = LEASE - LEASE_MARGIN
# 90 days of quarter-hour samples fit in 8.4 s on a development workstation (2026-11-20); the
# host is taken as up to 3.5 times slower.
CALIBRATION_FIT = timedelta(seconds=30)
PROVIDER_REQUEST = timedelta(seconds=HTTP_BODY_SECONDS + HTTP_READ_SECONDS)
# The last moment after its start at which a job may send a provider request: the request and the
# commit after it still end before the time limit.
LAST_REQUEST_START = TIME_LIMIT - PROVIDER_REQUEST - timedelta(seconds=DATABASE_STATEMENT_SECONDS)
# A mail actor past its transmit marker finishes its exchange, bounded per socket operation.
MAIL_EXCHANGE = timedelta(minutes=5)


@dataclass(frozen=True)
class ActorBound:
    lease: timedelta
    # None: never interrupted, because a send stopped after its transmit marker has no outcome.
    time_limit: timedelta | None

    @property
    def longest_run(self) -> timedelta:
        if self.time_limit is None:
            return self.lease + MAIL_EXCHANGE
        return self.time_limit + LEASE_MARGIN


ACTORS: dict[str, ActorBound] = {
    "collect_forecast": ActorBound(LEASE, TIME_LIMIT),
    "collect_epias": ActorBound(LEASE, TIME_LIMIT),
    "calibrate_plant": ActorBound(LEASE, TIME_LIMIT),
    "send_forecast_reminder": ActorBound(LEASE, None),
    "send_outbound_mail": ActorBound(LEASE, None),
}
DRAIN = max(bound.longest_run for bound in ACTORS.values())
# deploy/compose.yaml and the release scripts stop the background services within this.
STOP_GRACE = DRAIN + timedelta(minutes=1)
DISPATCH_POLL_SECONDS = 30.0
HEARTBEAT_LAG = timedelta(minutes=5)
PUBLISH_BATCH = 100
QUEUE_DEPTH_LIMIT = 10 * PUBLISH_BATCH


def time_limit_ms(name: str) -> float:
    limit = ACTORS[name].time_limit
    return math.inf if limit is None else limit.total_seconds() * 1000
