import json
import math
import re
from datetime import UTC, date, datetime, timedelta
from typing import Literal, cast
from zoneinfo import ZoneInfo

import httpx

from pyracast.connections.contracts import ProviderAdapter, ProviderData, ProviderError
from pyracast.connections.models import SiteBinding
from pyracast.connections.secrets import resolve_solcast_secret


def normalize_payload(
    payload: object,
    endpoint: Literal["forecasts", "estimated_actuals"] = "forecasts",
    *,
    collected_at: datetime | None = None,
) -> ProviderData:
    if endpoint not in ("forecasts", "estimated_actuals"):
        raise ProviderError("invalid_provider_endpoint")
    if not isinstance(payload, dict) or not isinstance(payload.get(endpoint), list):
        raise ProviderError("invalid_provider_schema")
    intervals: list[dict[str, object]] = []
    for row in payload[endpoint]:
        if not isinstance(row, dict) or row.get("period") != "PT30M":
            raise ProviderError("invalid_provider_interval")
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
        except KeyError, TypeError, ValueError, OverflowError:
            raise ProviderError("invalid_provider_interval") from None
        intervals.append(
            {
                "start": (end - timedelta(minutes=30)).isoformat(),
                "end": end.isoformat(),
                "power_kw": power,
                "sunlight": "unknown",
            }
        )
    try:
        json.dumps(payload, allow_nan=False)
    except TypeError, ValueError:
        raise ProviderError("invalid_provider_schema") from None
    # The Hobby response does not promise an issue timestamp or P50 semantics.
    return ProviderData(
        raw_payload=cast(dict[str, object], payload.copy()),
        intervals=intervals,
        collected_at=collected_at if collected_at is not None else datetime.now(UTC),
        source_kind="forecast" if endpoint == "forecasts" else "estimated_actuals",
    )


def _contains_secret(value: object, secret: str) -> bool:
    if isinstance(value, str):
        return secret in value
    if isinstance(value, list):
        return any(_contains_secret(item, secret) for item in value)
    if isinstance(value, dict):
        return any(
            _contains_secret(key, secret) or _contains_secret(item, secret)
            for key, item in value.items()
        )
    return False


class FixtureProvider:
    def validate(self, binding: SiteBinding) -> None:
        if binding.provider_account.provider != "fixture":
            raise ProviderError("fixture_account_required")

    def fetch(self, binding: SiteBinding, target_date: date) -> ProviderData:
        self.validate(binding)
        zone = ZoneInfo(binding.timezone)
        start = datetime.combine(target_date, datetime.min.time(), zone).astimezone(UTC)
        stop = datetime.combine(target_date + timedelta(days=1), datetime.min.time(), zone)
        rows: list[dict[str, object]] = []
        sunlight: list[str] = []
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
        for interval, light in zip(result.intervals, sunlight, strict=True):
            interval["sunlight"] = light
        return result

    def fetch_history(self, binding: SiteBinding, target_date: date, days: int) -> ProviderData:
        intervals: list[dict[str, object]] = []
        rows: list[object] = []
        for offset in reversed(range(days)):
            data = self.fetch(binding, target_date - timedelta(days=offset))
            intervals.extend(data.intervals)
            rows.extend(cast(list[object], data.raw_payload["forecasts"]))
        return ProviderData(
            {"estimated_actuals": rows, "synthetic": True},
            intervals,
            collected_at=datetime.now(UTC),
            source_kind="estimated_actuals",
        )


class SolcastProvider:
    def __init__(self, transport: httpx.BaseTransport | None = None) -> None:
        self.transport = transport

    def validate(self, binding: SiteBinding) -> None:
        # Saved Toolkit settings require operator attestation; the response only validates access.
        self.fetch(binding, datetime.now(UTC).date())

    def fetch(self, binding: SiteBinding, target_date: date) -> ProviderData:
        return self._request(binding, "forecasts")

    def fetch_history(self, binding: SiteBinding, target_date: date, days: int) -> ProviderData:
        return self._request(binding, "estimated_actuals")

    def _request(
        self, binding: SiteBinding, endpoint: Literal["forecasts", "estimated_actuals"]
    ) -> ProviderData:
        secret = resolve_solcast_secret(binding.provider_account.secret_reference)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", binding.site_id):
            raise ProviderError("invalid_site_id")
        try:
            with httpx.Client(
                timeout=httpx.Timeout(20, connect=5),
                transport=self.transport,
                follow_redirects=False,
            ) as client:
                response = client.get(
                    f"https://api.solcast.com.au/rooftop_sites/{binding.site_id}/{endpoint}",
                    headers={"Authorization": f"Bearer {secret}"},
                    params={"format": "json", "hours": 72 if endpoint == "forecasts" else 168},
                )
                response.raise_for_status()
                if secret in response.text:
                    raise ProviderError("unsafe_provider_response")
                payload = response.json()
        except httpx.HTTPError, json.JSONDecodeError, UnicodeDecodeError:
            raise ProviderError("provider_request_failed") from None
        if _contains_secret(payload, secret):
            raise ProviderError("unsafe_provider_response")
        return normalize_payload(payload, endpoint)


def adapter_for(binding: SiteBinding) -> ProviderAdapter:
    if binding.provider_account.provider == "fixture":
        return FixtureProvider()
    if binding.provider_account.provider == "solcast":
        return SolcastProvider()
    raise ProviderError("unsupported_provider")
