from collections.abc import Callable
from dataclasses import replace
from datetime import date, datetime

import pytest
from django.db import connection

from pyracast.connections.contracts import ProviderData
from pyracast.connections.models import SiteBinding
from pyracast.providers import FixtureProvider
from tests.support.domain import NOW


class HistoricalFixture(FixtureProvider):
    def __init__(self, now: datetime = NOW, action: Callable[[], None] = lambda: None) -> None:
        self.now = now
        self.action = action
        self.days: list[int] = []

    def fetch_history(self, binding: SiteBinding, target_date: date, days: int) -> ProviderData:
        assert not connection.in_atomic_block
        self.action()
        self.days.append(days)
        return replace(super().fetch_history(binding, target_date, days), collected_at=self.now)


def no_calculation(
    binding: SiteBinding, target_date: date, data: ProviderData
) -> dict[str, object]:
    pytest.fail("Estimated actuals must not enter forecast calculation")
