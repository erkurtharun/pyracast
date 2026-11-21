from collections.abc import Callable
from unittest.mock import MagicMock

import pytest
from dramatiq.brokers.stub import StubBroker

from pyracast.background.deadlines import ACTORS
from pyracast.background.failures import PhaseOutcome
from pyracast.forecasts.models import DispatcherPhase
from pyracast.orchestration import dispatcher, kinds
from pyracast.orchestration.kinds import KINDS, QUEUES, WORKER_QUEUES, register_actors


def test_each_job_kind_has_one_actor_on_its_queue_that_dramatiq_never_retries() -> None:
    actors = register_actors(StubBroker())

    assert set(actors) == {kind.actor for kind in KINDS} == set(ACTORS)
    for kind in KINDS:
        assert actors[kind.actor].queue_name == kind.queue
        assert actors[kind.actor].options["max_retries"] == 0
    assert set(QUEUES) == {kind.queue for kind in KINDS}
    assert set(WORKER_QUEUES) == set(QUEUES) - {kinds.MAIL_QUEUE}


def test_the_dispatcher_publishes_every_job_kind_in_exactly_one_stored_phase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: dict[str, list[object]] = {}

    def publisher(name: str, result: object) -> Callable[..., object]:
        def publish(*actors: object) -> object:
            received[name] = list(actors)
            return result

        return publish

    for name, result in (
        ("dispatch_notifications", 0),
        ("dispatch_collections", PhaseOutcome(published=0)),
        ("dispatch_revalidations", 0),
        ("dispatch_calibrations", 0),
        ("dispatch_epias", 0),
    ):
        monkeypatch.setattr(dispatcher, name, publisher(name, result))
    for step in ("purge_expired", "warn_before_purge", "sweep_accounts", "purge_finished_mail"):
        monkeypatch.setattr(dispatcher, step, MagicMock())
    actors = {kind.actor: kind.make_actor(StubBroker()) for kind in KINDS}

    phases = dispatcher.phases(actors)
    for _name, operation in phases:
        operation()

    assert [name for name, _operation in phases] == [
        "notifications",
        "collection",
        "calibration",
        "retention",
        "metering",
    ]
    assert {name for name, _operation in phases} == set(DispatcherPhase.values)
    published = [actor for given in received.values() for actor in given]
    assert sorted(map(id, published)) == sorted(map(id, actors.values()))
