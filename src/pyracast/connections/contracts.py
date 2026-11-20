from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal, Protocol, runtime_checkable

from pyracast.connections.models import SiteBinding
from pyracast.forecasts.models import SnapshotWeatherError
from pyracast.intervals import NormalizedInterval
from pyracast.weather_data import WeatherData


@dataclass(frozen=True)
class ProviderData:
    raw_payload: dict[str, object]
    intervals: tuple[NormalizedInterval, ...]
    issued_at: datetime | None = None
    collected_at: datetime | None = None
    source_kind: Literal["forecast", "estimated_actuals"] = "forecast"
    weather: WeatherData | None = None
    weather_error: SnapshotWeatherError | Literal[""] = ""


class ProviderError(Exception):
    def __init__(
        self,
        code: str,
        *,
        retryable: bool = True,
        retry_after_seconds: int | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds
        self.status_code = status_code


class ProviderAdapter(Protocol):
    # Local checks that run before a ledger request is reserved; raise a non-retryable error.
    def prepare(self, binding: SiteBinding) -> None: ...

    def validate(self, binding: SiteBinding) -> None: ...

    def fetch(self, binding: SiteBinding, target_date: date) -> ProviderData: ...


@runtime_checkable
class HistoryAdapter(Protocol):
    def fetch_history(self, binding: SiteBinding, target_date: date, days: int) -> ProviderData: ...


@runtime_checkable
class WindowHistoryAdapter(Protocol):
    def fetch_history_window(
        self, binding: SiteBinding, start: datetime, end: datetime, now: datetime
    ) -> ProviderData: ...


Calculator = Callable[[SiteBinding, date, ProviderData], dict[str, object]]
