import logging
from datetime import UTC, date, datetime, timedelta
from math import isclose, isfinite
from typing import cast
from zoneinfo import ZoneInfo

from pyracast.connections.contracts import Calculator, ProviderData
from pyracast.connections.models import SiteBinding
from pyracast.forecasts.models import ForecastResult, ForecastSnapshot
from pyracast.irradiance import local_day_bounds

logger = logging.getLogger(__name__)


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("A saved interval timestamp must be an ISO string.")
    result = datetime.fromisoformat(value)
    if result.utcoffset() is None:
        raise ValueError("A saved interval timestamp must be timezone-aware.")
    return result


def prepare_daily_payloads(
    binding: SiteBinding,
    target_date: date,
    data: ProviderData,
    calculator: Calculator,
    target_payload: dict[str, object],
) -> dict[str, dict[str, object]]:
    payloads = {target_date.isoformat(): target_payload}
    if data.collected_at is None:
        return payloads
    zone = ZoneInfo(binding.timezone)
    first = data.collected_at.astimezone(zone).date()
    # Bound preparation by the provider's elapsed 72-hour retrieval horizon, including DST.
    horizon = data.collected_at.astimezone(UTC) + timedelta(hours=72, microseconds=-1)
    last = horizon.astimezone(zone).date()
    days: set[date] = set()
    for row in data.intervals:
        try:
            start, end = _timestamp(row.get("start")), _timestamp(row.get("end"))
        except ValueError:
            continue
        if end <= start:
            continue
        day = max(first, start.astimezone(zone).date())
        final = min(last, (end - timedelta(microseconds=1)).astimezone(zone).date())
        while day <= final:
            days.add(day)
            day += timedelta(days=1)
    for day in sorted(days - {target_date}):
        try:
            payloads[day.isoformat()] = calculator(binding, day, data)
        except Exception as error:
            logger.warning(
                "Supplemental forecast calculation failed local_date=%s error_type=%s",
                day.isoformat(),
                type(error).__name__,
            )
    return payloads


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError("A saved forecast value must be numeric.")
    if not isfinite(value) or value < 0:
        raise ValueError("A saved forecast value must be finite and nonnegative.")
    return float(value)


def _flags(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(flag, str) for flag in value)


def _valid_day(value: object, binding: SiteBinding, target_date: date) -> bool:
    if not isinstance(value, dict):
        return False
    vintage = value.get("vintage")
    if (
        value.get("local_date") != target_date.isoformat()
        or value.get("timezone") != binding.timezone
        or value.get("region_id") != binding.region_id
        or not isinstance(value.get("complete"), bool)
        or not isinstance(value.get("model_version"), str)
        or not value.get("model_version")
        or not _flags(value.get("quality_flags"))
        or not isinstance(value.get("intervals"), list)
        or not isinstance(vintage, dict)
        or vintage.get("binding_revision") != f"{binding.pk}:{binding.region_revision}"
        or vintage.get("source_kind") != "forecast"
    ):
        return False
    try:
        start, end = local_day_bounds(target_date, binding.timezone)
        if _timestamp(value.get("start")) != start or _timestamp(value.get("end")) != end:
            return False
        duration = (end - start).total_seconds()
        valid = _number(value.get("valid_coverage_seconds"))
        coverage = _number(value.get("coverage_seconds"))
        fraction = _number(value.get("coverage_fraction"))
        partial = _number(value.get("partial_energy_kwh_m2"))
        if not valid <= coverage <= duration or fraction > 1:
            return False
        if value["complete"]:
            total = _number(value.get("energy_kwh_m2"))
            if valid != duration or fraction != 1 or total != partial:
                return False
        elif value.get("energy_kwh_m2") is not None or valid == duration:
            return False
        previous = start
        interval_coverage = interval_valid = timedelta()
        interval_energy = 0.0
        for row in value["intervals"]:
            if not isinstance(row, dict) or not _flags(row.get("quality_flags")):
                return False
            left = max(start, _timestamp(row.get("start")))
            right = min(end, _timestamp(row.get("end")))
            if not previous <= left < right <= end:
                return False
            if row.get("ghi_wm2") is not None:
                ghi = _number(row["ghi_wm2"])
                interval_valid += right - left
                interval_energy += ghi * ((right - left).total_seconds() / 3_600_000)
            interval_coverage += right - left
            previous = right
        if (
            interval_coverage.total_seconds() != coverage
            or interval_valid.total_seconds() != valid
            or not isclose(fraction, valid / duration, abs_tol=1e-12)
            or not isclose(partial, interval_energy, abs_tol=1e-12)
        ):
            return False
    except ValueError, ArithmeticError:
        return False
    return True


def saved_day_payload(snapshot: ForecastSnapshot, target_date: date) -> dict[str, object] | None:
    try:
        result = snapshot.forecastresult
    except ForecastResult.DoesNotExist:
        return None
    daily_payloads = result.daily_payloads
    candidate = (
        daily_payloads.get(target_date.isoformat()) if isinstance(daily_payloads, dict) else None
    )
    for payload in (candidate, result.payload):
        if _valid_day(payload, snapshot.binding, target_date):
            return cast(dict[str, object], payload)
    return None
