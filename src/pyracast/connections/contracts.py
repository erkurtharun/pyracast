from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal, Protocol, runtime_checkable

from pyracast.connections.models import SiteBinding
from pyracast.weather import WeatherData


@dataclass(frozen=True)
class ProviderData:
    raw_payload: dict[str, object]
    intervals: list[dict[str, object]]
    issued_at: datetime | None = None
    collected_at: datetime | None = None
    source_kind: Literal["forecast", "estimated_actuals"] = "forecast"
    weather: WeatherData | None = None
    weather_error: str = ""


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
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds
        self.status_code = status_code


class ProviderAdapter(Protocol):
    def validate(self, binding: SiteBinding) -> None: ...

    def fetch(self, binding: SiteBinding, target_date: date) -> ProviderData: ...


@runtime_checkable
class HistoryAdapter(Protocol):
    def fetch_history(self, binding: SiteBinding, target_date: date, days: int) -> ProviderData: ...


Calculator = Callable[[SiteBinding, date, ProviderData], dict[str, object]]
