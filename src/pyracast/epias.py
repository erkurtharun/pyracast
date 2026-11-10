import json
import math
import re
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from email.utils import parsedate_to_datetime
from threading import Lock
from typing import cast
from zoneinfo import ZoneInfo

import httpx

AUTH_URL = "https://giris.epias.com.tr/cas/v1/tickets"
BASE_URL = "https://seffaflik.epias.com.tr/electricity-service/v1/generation/data"
TIMEZONE = ZoneInfo("Europe/Istanbul")


class EpiasError(Exception):
    def __init__(self, code: str, *, retry_after_seconds: int | None = None) -> None:
        super().__init__(code)
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True)
class EpiasPlant:
    id: int
    eic: str | None
    name: str
    short_name: str


@dataclass(frozen=True)
class EpiasReading:
    timestamp: datetime
    hour: str
    total: Decimal | None
    sun: Decimal | None


@dataclass(frozen=True)
class EpiasDay:
    plant: EpiasPlant
    day: date
    readings: tuple[EpiasReading, ...]
    collected_at: datetime
    raw_payload: str
    endpoint: str = f"{BASE_URL}/realtime-generation"
    unit: str = "MWh"


def _retry_after(value: str | None) -> int | None:
    if value is None or len(value) > 128:
        return None
    try:
        seconds = (
            int(value)
            if value.isascii() and value.isdecimal()
            else math.ceil((parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds())
        )
    except ValueError, TypeError, OverflowError:
        return None
    return max(0, min(86400, seconds))


def _has_secret(value: object, secrets: tuple[str, ...]) -> bool:
    if isinstance(value, str):
        return any(secret in value for secret in secrets)
    if isinstance(value, list):
        return any(_has_secret(item, secrets) for item in value)
    if isinstance(value, dict):
        return any(
            _has_secret(key, secrets) or _has_secret(item, secrets) for key, item in value.items()
        )
    return False


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise EpiasError("invalid_epias_schema")
    return cast(dict[str, object], value)


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise EpiasError("invalid_epias_schema")
    return value


def _text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EpiasError("invalid_epias_schema")
    return value


def _energy(value: object) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float | Decimal):
        raise EpiasError("invalid_epias_energy")
    result = Decimal(str(value))
    if not result.is_finite() or result < 0:
        raise EpiasError("invalid_epias_energy")
    return result


def _reading(row: dict[str, object], day: date, plant: EpiasPlant) -> EpiasReading:
    try:
        start = datetime.fromisoformat(_text(row.get("date")))
    except ValueError:
        raise EpiasError("invalid_epias_interval") from None
    if start.utcoffset() is None:
        raise EpiasError("invalid_epias_interval")
    start = start.astimezone(TIMEZONE)
    if start.date() != day or start.minute or start.second or start.microsecond:
        raise EpiasError("invalid_epias_interval")
    if row.get("hour") != start.strftime("%H:%M"):
        raise EpiasError("invalid_epias_interval")
    if "powerPlantId" in row and _integer(row["powerPlantId"]) != plant.id:
        raise EpiasError("invalid_epias_plant")
    return EpiasReading(
        start, start.strftime("%H:%M"), _energy(row.get("total")), _energy(row.get("sun"))
    )


class EpiasClient:
    def __init__(
        self, client: httpx.Client, *, username: str, password: str, page_size: int = 100
    ) -> None:
        if not username or not password:
            raise EpiasError("missing_epias_credentials")
        if isinstance(page_size, bool) or not 1 <= page_size <= 100:
            raise EpiasError("invalid_epias_page_size")
        self._client = client
        self._username = username
        self._password = password
        self._page_size = page_size
        self._ticket = ""
        self._ticket_expires = 0.0
        self._ticket_lock = Lock()

    def _request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        body: dict[str, object] | None = None,
        form: dict[str, str] | None = None,
        expected_status: int = 200,
    ) -> httpx.Response:
        try:
            response = self._client.request(
                method,
                url,
                headers=headers,
                json=body,
                data=form,
                timeout=httpx.Timeout(20, connect=5),
                follow_redirects=False,
                auth=None,
            )
        except httpx.HTTPError:
            raise EpiasError("epias_request_failed") from None
        if response.status_code != expected_status:
            if response.status_code in (401, 403):
                self._ticket = ""
            raise EpiasError(
                "epias_request_failed",
                retry_after_seconds=(
                    _retry_after(response.headers.get("Retry-After"))
                    if response.status_code in (429, 503)
                    else None
                ),
            )
        return response

    def _tgt(self) -> str:
        with self._ticket_lock:
            if self._ticket and time.monotonic() < self._ticket_expires:
                return self._ticket
            response = self._request(
                "POST",
                AUTH_URL,
                headers={"Accept": "text/plain"},
                form={"username": self._username, "password": self._password},
                expected_status=201,
            )
            ticket = response.text.strip()
            if not re.fullmatch(r"TGT-[A-Za-z0-9._-]{1,4090}", ticket):
                raise EpiasError("invalid_epias_ticket")
            self._ticket = ticket
            self._ticket_expires = time.monotonic() + 7140
            return ticket

    def _data(self, endpoint: str, body: dict[str, object] | None = None) -> dict[str, object]:
        ticket = self._tgt()
        response = self._request(
            "GET" if body is None else "POST",
            f"{BASE_URL}/{endpoint}",
            headers={"TGT": ticket, "Accept": "application/json"},
            body=body,
        )
        try:
            payload: object = json.loads(response.content)
        except ValueError, UnicodeDecodeError:
            raise EpiasError("invalid_epias_schema") from None
        if _has_secret(payload, (self._username, self._password, ticket)):
            raise EpiasError("unsafe_epias_response")
        return _object(payload)

    def list_plants(self) -> tuple[EpiasPlant, ...]:
        items = self._data("powerplant-list").get("items")
        if not isinstance(items, list) or len(items) > 100_000:
            raise EpiasError("invalid_epias_schema")
        result: list[EpiasPlant] = []
        ids: set[int] = set()
        for item in items:
            row = _object(item)
            eic = row.get("eic")
            plant = EpiasPlant(
                _integer(row.get("id")),
                _text(eic) if eic is not None else None,
                _text(row.get("name")),
                _text(row.get("shortName")),
            )
            if plant.id == 0 or plant.id in ids:
                raise EpiasError("invalid_epias_plant")
            ids.add(plant.id)
            result.append(plant)
        return tuple(result)

    def fetch_day(self, plant: EpiasPlant, day: date) -> EpiasDay:
        if _integer(plant.id) == 0:
            raise EpiasError("invalid_epias_plant")
        start = datetime.combine(day, datetime.min.time(), TIMEZONE)
        stop = datetime.combine(day + timedelta(days=1), datetime.min.time(), TIMEZONE)
        expected_hours = int((stop.astimezone(UTC) - start.astimezone(UTC)).total_seconds() / 3600)
        readings: list[EpiasReading] = []
        safe_rows: list[dict[str, object]] = []
        total: int | None = None
        number = 1
        while True:
            payload = self._data(
                "realtime-generation",
                {
                    "startDate": start.isoformat(),
                    "endDate": (stop - timedelta(hours=1)).isoformat(),
                    "powerPlantId": plant.id,
                    "page": {"number": number, "size": self._page_size},
                },
            )
            page = _object(payload.get("page"))
            current_total = _integer(page.get("total"))
            if (
                _integer(page.get("number")) != number
                or _integer(page.get("size")) != self._page_size
                or current_total > expected_hours
                or (total is not None and current_total != total)
            ):
                raise EpiasError("invalid_epias_pagination")
            total = current_total
            items = payload.get("items")
            expected_count = min(self._page_size, total - len(readings))
            if not isinstance(items, list) or len(items) != expected_count:
                raise EpiasError("invalid_epias_pagination")
            for item in items:
                row = _object(item)
                readings.append(_reading(row, day, plant))
                safe_rows.append(
                    {key: row[key] for key in ("date", "hour", "total", "sun") if key in row}
                )
            if len(readings) == total:
                break
            number += 1
        ordered = tuple(sorted(readings, key=lambda row: row.timestamp))
        if len({reading.timestamp for reading in ordered}) != len(ordered):
            raise EpiasError("duplicate_epias_timestamps")
        return EpiasDay(
            plant,
            day,
            ordered,
            datetime.now(UTC),
            json.dumps({"items": safe_rows}, ensure_ascii=False, allow_nan=False),
        )
