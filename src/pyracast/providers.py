import json
import math
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from typing import Literal, cast
from zoneinfo import ZoneInfo

import httpx
from django.conf import settings
from django.views.decorators.debug import sensitive_variables

from pyracast.connections.contracts import (
    HistoryAdapter,
    ProviderAdapter,
    ProviderData,
    ProviderError,
    WindowHistoryAdapter,
)
from pyracast.connections.models import SITE_ID_PATTERN, SiteBinding
from pyracast.connections.secrets import decrypt_api_key
from pyracast.intervals import NormalizedInterval, Sunlight
from pyracast.irradiance import local_day_bounds
from pyracast.outbound import TIMEOUT, UnboundedResponseError, read_bounded
from pyracast.provider_retry import retry_after_seconds, retryable_status


def clip_provider_data(data: ProviderData, start: datetime, end: datetime) -> ProviderData:
    intervals: list[NormalizedInterval] = []
    for interval in data.intervals:
        if interval.start >= end or interval.end <= start:
            continue
        if interval.start < start or interval.end > end:
            interval = replace(
                interval,
                original_start=interval.original_start or interval.start,
                original_end=interval.original_end or interval.end,
                start=max(interval.start, start),
                end=min(interval.end, end),
            )
        intervals.append(interval)
    return replace(data, intervals=tuple(intervals))


def normalize_payload(
    payload: object,
    endpoint: Literal["forecasts", "estimated_actuals"] = "forecasts",
    *,
    collected_at: datetime | None = None,
) -> ProviderData:
    if endpoint not in ("forecasts", "estimated_actuals"):
        raise ProviderError("invalid_provider_endpoint", retryable=False)
    if not isinstance(payload, dict) or not isinstance(payload.get(endpoint), list):
        raise ProviderError("invalid_provider_schema", retryable=False)
    intervals: list[NormalizedInterval] = []
    for row in payload[endpoint]:
        if not isinstance(row, dict) or row.get("period") != "PT30M":
            raise ProviderError("invalid_provider_interval", retryable=False)
        try:
            end = datetime.fromisoformat(row["period_end"])
            if end.utcoffset() is None:
                raise ValueError("Missing UTC offset")
            power = row.get("pv_estimate")
            if power is not None and (
                isinstance(power, bool) or not isinstance(power, int | float)
            ):
                raise ValueError("Invalid power type")
            if power is not None and not math.isfinite(power):
                raise ValueError("Nonfinite power")
            end = end.astimezone(UTC)
            start = end - timedelta(minutes=30)
        except KeyError, TypeError, ValueError, OverflowError:
            raise ProviderError("invalid_provider_interval", retryable=False) from None
        intervals.append(NormalizedInterval(start, end, power))
    try:
        # PostgreSQL jsonb stores neither NUL nor unpaired surrogates.
        json.dumps(payload, allow_nan=False, ensure_ascii=False).encode("utf-8")
    except TypeError, ValueError:
        raise ProviderError("invalid_provider_schema", retryable=False) from None
    if contains_text(payload, "\u0000"):
        raise ProviderError("invalid_provider_schema", retryable=False)
    # The Hobby response does not promise an issue timestamp or P50 semantics.
    return ProviderData(
        raw_payload=payload.copy(),
        intervals=tuple(intervals),
        collected_at=collected_at if collected_at is not None else datetime.now(UTC),
        source_kind="forecast" if endpoint == "forecasts" else "estimated_actuals",
    )


def contains_text(value: object, text: str) -> bool:
    if isinstance(value, str):
        return text in value
    if isinstance(value, list):
        return any(contains_text(item, text) for item in value)
    if isinstance(value, dict):
        return any(
            contains_text(key, text) or contains_text(item, text) for key, item in value.items()
        )
    return False


class FixtureProvider(ProviderAdapter, HistoryAdapter):
    def prepare(self, binding: SiteBinding) -> None:
        pass

    def validate(self, binding: SiteBinding) -> None:
        if binding.provider_account.provider != "fixture":
            raise ProviderError("fixture_account_required")

    def fetch(self, binding: SiteBinding, target_date: date) -> ProviderData:
        self.validate(binding)
        zone = ZoneInfo(binding.timezone)
        start = datetime.combine(target_date, datetime.min.time(), zone).astimezone(UTC)
        stop = datetime.combine(target_date + timedelta(days=1), datetime.min.time(), zone)
        rows: list[dict[str, object]] = []
        sunlight: list[Sunlight] = []
        while start < stop.astimezone(UTC):
            hour = (start + timedelta(minutes=15)).astimezone(zone).hour
            day = 6 <= hour < 18
            rows.append(
                {
                    "period_end": (start + timedelta(minutes=30)).isoformat(),
                    "period": "PT30M",
                    "pv_estimate": binding.capacity_kw * 0.6 if day else 0.0,
                }
            )
            sunlight.append("day" if day else "night")
            start += timedelta(minutes=30)
        result = normalize_payload({"forecasts": rows, "synthetic": True})
        return replace(
            result,
            intervals=tuple(
                replace(interval, sunlight=light)
                for interval, light in zip(result.intervals, sunlight, strict=True)
            ),
        )

    def fetch_history(self, binding: SiteBinding, target_date: date, days: int) -> ProviderData:
        intervals: list[NormalizedInterval] = []
        rows: list[object] = []
        for offset in reversed(range(days)):
            data = self.fetch(binding, target_date - timedelta(days=offset))
            intervals.extend(data.intervals)
            rows.extend(cast(list[object], data.raw_payload["forecasts"]))
        return ProviderData(
            {"estimated_actuals": rows, "synthetic": True},
            tuple(intervals),
            collected_at=datetime.now(UTC),
            source_kind="estimated_actuals",
        )


class SolcastProvider(ProviderAdapter, WindowHistoryAdapter):
    def __init__(
        self,
        transport: httpx.BaseTransport | None = None,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.transport = transport
        self.clock = clock

    def prepare(self, binding: SiteBinding) -> None:
        try:
            decrypt_api_key(binding.provider_account.encrypted_api_key)
        except ProviderError:
            raise ProviderError("invalid_provider_secret", retryable=False) from None
        if not re.fullmatch(SITE_ID_PATTERN, binding.site_id):
            raise ProviderError("invalid_site_id", retryable=False)

    def validate(self, binding: SiteBinding) -> None:
        # Saved Toolkit settings require operator attestation; the response only validates access.
        self.fetch(binding, datetime.now(UTC).date())

    def fetch(self, binding: SiteBinding, target_date: date) -> ProviderData:
        now = self.clock()
        today = now.astimezone(ZoneInfo(binding.timezone)).date()
        start, _ = local_day_bounds(today, binding.timezone)
        end, _ = local_day_bounds(today + timedelta(days=3), binding.timezone)
        hours = max(1, math.ceil((end - now).total_seconds() / 3600))
        return clip_provider_data(self._request(binding, "forecasts", hours), start, end)

    def fetch_history_window(
        self, binding: SiteBinding, start: datetime, end: datetime, now: datetime
    ) -> ProviderData:
        hours = min(168, max(1, math.ceil((now - start).total_seconds() / 3600)))
        return clip_provider_data(self._request(binding, "estimated_actuals", hours), start, end)

    def _request(
        self, binding: SiteBinding, endpoint: Literal["forecasts", "estimated_actuals"], hours: int
    ) -> ProviderData:
        self.prepare(binding)
        answer = solcast_get(
            decrypt_api_key(binding.provider_account.encrypted_api_key),
            f"/rooftop_sites/{binding.site_id}/{endpoint}",
            {"hours": hours},
            transport=self.transport,
        )
        return normalize_payload(answer.payload, endpoint, collected_at=self.clock())


@dataclass(frozen=True)
class SolcastAnswer:
    payload: object
    headers: httpx.Headers


@sensitive_variables()
def solcast_get(
    secret: str,
    path: str,
    params: dict[str, str | int],
    *,
    transport: httpx.BaseTransport | None = None,
) -> SolcastAnswer:
    started = time.monotonic()
    try:
        with (
            httpx.Client(timeout=TIMEOUT, transport=transport, follow_redirects=False) as client,
            client.stream(
                "GET",
                f"https://api.solcast.com.au{path}",
                headers={"Authorization": f"Bearer {secret}"},
                params={"format": "json", **params},
            ) as response,
        ):
            if not response.is_success:
                raise ProviderError(
                    "provider_request_failed",
                    retryable=retryable_status(response.status_code),
                    retry_after_seconds=retry_after_seconds(response.headers.get("Retry-After"))
                    if response.status_code in {429, 503}
                    else None,
                    status_code=response.status_code,
                ) from None
            body = read_bounded(response, started).decode("utf-8")
            headers = response.headers
    except UnboundedResponseError as exc:
        if exc.too_large:
            raise ProviderError("provider_response_too_large", retryable=False) from None
        raise ProviderError("provider_request_failed") from None
    except httpx.DecodingError, UnicodeDecodeError:
        raise ProviderError("invalid_provider_schema") from None
    except httpx.HTTPError:
        raise ProviderError("provider_request_failed") from None
    if secret in body:
        raise ProviderError("unsafe_provider_response", retryable=False)
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise ProviderError("invalid_provider_schema") from None
    if contains_text(payload, secret) or contains_text(dict(headers), secret):
        raise ProviderError("unsafe_provider_response", retryable=False)
    return SolcastAnswer(payload, headers)


def adapter_for(binding: SiteBinding) -> ProviderAdapter:
    if binding.provider_account.provider == "fixture":
        if not settings.PYRACAST_FIXTURE_PROVIDER:
            raise ProviderError("fixture_provider_disabled")
        return FixtureProvider()
    if binding.provider_account.provider == "solcast":
        return SolcastProvider()
    raise ProviderError("unsupported_provider")
