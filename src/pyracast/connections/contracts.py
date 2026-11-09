from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal, Protocol

from pyracast.connections.models import SiteBinding


@dataclass(frozen=True)
class ProviderData:
    raw_payload: dict[str, object]
    intervals: list[dict[str, object]]
    issued_at: datetime | None = None
    collected_at: datetime | None = None
    source_kind: Literal["forecast", "estimated_actuals"] = "forecast"


class ProviderFailure(Exception):
    pass


class ProviderAdapter(Protocol):
    def validate(self, binding: SiteBinding) -> None: ...

    def fetch(self, binding: SiteBinding, target_date: date) -> ProviderData: ...


Calculator = Callable[[SiteBinding, date, ProviderData], dict[str, object]]
