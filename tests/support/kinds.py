from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock

import pytest

from pyracast.orchestration import kinds


class StubKind:
    def __init__(self, kind: kinds.JobKind, make_actor: Callable[[Any], object]) -> None:
        self.actor = kind.actor
        self.queue = kind.queue
        self.make_actor = make_actor


def stub_actors(monkeypatch: pytest.MonkeyPatch, **made: Callable[[Any], object]) -> None:
    """Registers each job kind's actor through the factory named by its actor, else a mock."""
    monkeypatch.setattr(
        kinds,
        "KINDS",
        tuple(StubKind(kind, made.get(kind.actor, MagicMock())) for kind in kinds.KINDS),
    )
