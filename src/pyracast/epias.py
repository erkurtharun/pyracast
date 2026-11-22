import json
import re
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from threading import Lock
from typing import Literal, NoReturn
from zoneinfo import ZoneInfo

import httpx

from pyracast import provider_retry
from pyracast.outbound import TIMEOUT, UnboundedResponseError, read_bounded

AUTH_URL = "https://giris.epias.com.tr/cas/v1/tickets"
# A plant list holds up to 100,000 plants.
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
BASE_URL = "https://seffaflik.epias.com.tr/electricity-service/v1/generation/data"
TIMEZONE = ZoneInfo("Europe/Istanbul")
SourceKind = Literal["realtime_generation", "settlement_injection"]
CREDENTIALS_MISSING = "epias_credentials_missing"
CREDENTIALS_REJECTED = "epias_credentials_rejected"


class EpiasError(Exception):
    def __init__(
        self, code: str, *, retry_after_seconds: int | None = None, retryable: bool = True
    ) -> None:
        super().__init__(code)
        self.retry_after_seconds = retry_after_seconds
        self.retryable = retryable


class EpiasCredentialsError(EpiasError):
    """Missing or rejected credentials: no data request was sent, and none can succeed until
    the process restarts with other credentials."""

    def __init__(self, code: str) -> None:
        super().__init__(code, retryable=False)


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

    @property
    def period_start(self) -> datetime:
        return self.timestamp

    @property
    def period_end(self) -> datetime:
        return self.timestamp + timedelta(hours=1)


@dataclass(frozen=True)
class EpiasDay:
    plant: EpiasPlant
    day: date
    readings: tuple[EpiasReading, ...]
    collected_at: datetime
    raw_payload: str
    endpoint: str = f"{BASE_URL}/realtime-generation"
    source_kind: SourceKind = "realtime_generation"
    end_day: date | None = None


def _before(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise EpiasError("epias_deadline_exceeded")


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
    return value


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


def _reading(
    row: dict[str, object],
    day: date,
    plant: EpiasPlant,
    source_kind: SourceKind = "realtime_generation",
    end_day: date | None = None,
) -> EpiasReading:
    try:
        start = datetime.fromisoformat(_text(row.get("date")))
    except ValueError:
        raise EpiasError("invalid_epias_interval") from None
    if start.utcoffset() is None:
        raise EpiasError("invalid_epias_interval")
    start = start.astimezone(TIMEZONE)
    if (
        not day <= start.date() <= (end_day or day)
        or start.minute
        or start.second
        or start.microsecond
    ):
        raise EpiasError("invalid_epias_interval")
    settlement = source_kind == "settlement_injection"
    hour = _integer(row.get("hour")) if settlement else row.get("hour")
    if hour != (start.hour if settlement else start.strftime("%H:%M")):
        raise EpiasError("invalid_epias_interval")
    identity_key = "powerplantId" if settlement else "powerPlantId"
    if identity_key in row and _integer(row[identity_key]) != plant.id:
        raise EpiasError("invalid_epias_plant")
    return EpiasReading(
        start,
        start.strftime("%H:%M"),
        _energy(row.get("total")),
        _energy(row.get("sun")),
    )


class EpiasClient:
    def __init__(
        self, client: httpx.Client, *, username: str, password: str, page_size: int = 100
    ) -> None:
        if not username or not password:
            raise EpiasCredentialsError(CREDENTIALS_MISSING)
        if isinstance(page_size, bool) or not 1 <= page_size <= 100:
            raise EpiasError("invalid_epias_page_size")
        self._client = client
        self._username = username
        self._password = password
        self._page_size = page_size
        self._ticket = ""
        self._ticket_expires = 0.0
        self._ticket_lock = Lock()
        self._rejected = False

    def _request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        body: dict[str, object] | None = None,
        form: dict[str, str] | None = None,
        expected_status: int = 200,
    ) -> bytes:
        started = time.monotonic()
        try:
            with self._client.stream(
                method,
                url,
                headers=headers,
                json=body,
                data=form,
                timeout=TIMEOUT,
                follow_redirects=False,
                auth=None,
            ) as response:
                if response.status_code != expected_status:
                    self._refuse(url, response)
                return read_bounded(response, started, limit=MAX_RESPONSE_BYTES)
        except UnboundedResponseError as error:
            raise EpiasError(
                "epias_response_too_large" if error.too_large else "epias_request_failed",
                retryable=not error.too_large,
            ) from None
        except httpx.HTTPError:
            raise EpiasError("epias_request_failed") from None

    def _refuse(self, url: str, response: httpx.Response) -> NoReturn:
        if response.status_code in (401, 403):
            self._ticket = ""
            if url == AUTH_URL:
                self._rejected = True
                raise EpiasCredentialsError(CREDENTIALS_REJECTED)
        raise EpiasError(
            "epias_request_failed",
            retryable=provider_retry.retryable_status(response.status_code)
            or response.status_code == 401,
            retry_after_seconds=(
                provider_retry.retry_after_seconds(response.headers.get("Retry-After"))
                if response.status_code in (429, 503)
                else None
            ),
        )

    def _tgt(self) -> str:
        with self._ticket_lock:
            if self._rejected:
                raise EpiasCredentialsError(CREDENTIALS_REJECTED)
            if self._ticket and time.monotonic() < self._ticket_expires:
                return self._ticket
            answer = self._request(
                "POST",
                AUTH_URL,
                headers={"Accept": "text/plain"},
                form={"username": self._username, "password": self._password},
                expected_status=201,
            )
            ticket = answer.decode("utf-8", "replace").strip()
            if not re.fullmatch(r"TGT-[A-Za-z0-9._-]{1,4090}", ticket):
                raise EpiasError("invalid_epias_ticket")
            self._ticket = ticket
            self._ticket_expires = time.monotonic() + 7140
            return ticket

    def _data(
        self, endpoint: str, body: dict[str, object] | None = None, deadline: float | None = None
    ) -> dict[str, object]:
        """``deadline`` is a ``time.monotonic()`` value after which no request is sent."""
        _before(deadline)
        ticket = self._tgt()
        _before(deadline)
        answer = self._request(
            "GET" if body is None else "POST",
            f"{BASE_URL}/{endpoint}",
            headers={"TGT": ticket, "Accept": "application/json"},
            body=body,
        )
        try:
            payload: object = json.loads(answer)
        except ValueError, UnicodeDecodeError:
            raise EpiasError("invalid_epias_schema") from None
        if _has_secret(payload, (self._username, self._password, ticket)):
            raise EpiasError("unsafe_epias_response")
        return _object(payload)

    def list_plants(self) -> tuple[EpiasPlant, ...]:
        return self._list_plants("powerplant-list")

    def list_settlement_plants(self) -> tuple[EpiasPlant, ...]:
        return self._list_plants("injection-quantity-powerplant-list")

    def resolve_settlement_plant(self, eic: str) -> EpiasPlant | None:
        matches = [plant for plant in self.list_settlement_plants() if plant.eic == eic]
        if len(matches) > 1:
            raise EpiasError("ambiguous_epias_eic")
        return matches[0] if matches else None

    def _list_plants(self, endpoint: str) -> tuple[EpiasPlant, ...]:
        items = self._data(endpoint).get("items")
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

    def fetch_day(self, plant: EpiasPlant, day: date, *, deadline: float | None = None) -> EpiasDay:
        return self._fetch_period(plant, day, day, "realtime_generation", deadline)

    def fetch_settlement(
        self, plant: EpiasPlant, start_day: date, end_day: date, *, deadline: float | None = None
    ) -> EpiasDay:
        if end_day < start_day or (end_day - start_day).days > 30:
            raise EpiasError("invalid_epias_period")
        return self._fetch_period(plant, start_day, end_day, "settlement_injection", deadline)

    def _fetch_period(
        self,
        plant: EpiasPlant,
        day: date,
        end_day: date,
        source_kind: SourceKind,
        deadline: float | None,
    ) -> EpiasDay:
        if _integer(plant.id) == 0:
            raise EpiasError("invalid_epias_plant")
        start = datetime.combine(day, datetime.min.time(), TIMEZONE)
        stop = datetime.combine(end_day + timedelta(days=1), datetime.min.time(), TIMEZONE)
        settlement = source_kind == "settlement_injection"
        endpoint = "injection-quantity" if settlement else "realtime-generation"
        expected_hours = int((stop.astimezone(UTC) - start.astimezone(UTC)).total_seconds() / 3600)
        readings: list[EpiasReading] = []
        safe_rows: list[dict[str, object]] = []
        total: int | None = None
        number = 1
        while True:
            payload = self._data(
                endpoint,
                {
                    "startDate": start.isoformat(),
                    "endDate": (stop - timedelta(hours=1)).isoformat(),
                    "powerplantId" if settlement else "powerPlantId": plant.id,
                    "page": {"number": number, "size": self._page_size},
                },
                deadline,
            )
            page = _object(payload.get("page"))
            if (
                _integer(page.get("number")) != number
                or _integer(page.get("size")) != self._page_size
            ):
                raise EpiasError("invalid_epias_pagination")
            items = payload.get("items")
            current_total = (
                0
                if settlement
                and number == 1
                and "total" in page
                and page["total"] is None
                and items == []
                else _integer(page.get("total"))
            )
            if current_total > expected_hours or (total is not None and current_total != total):
                raise EpiasError("invalid_epias_pagination")
            total = current_total
            expected_count = min(self._page_size, total - len(readings))
            if not isinstance(items, list) or len(items) != expected_count:
                raise EpiasError("invalid_epias_pagination")
            for item in items:
                row = _object(item)
                readings.append(_reading(row, day, plant, source_kind, end_day))
                safe_rows.append(
                    {
                        key: row[key]
                        for key in (
                            ("date", "hour", "total", "sun", "powerplantId")
                            if settlement
                            else ("date", "hour", "total", "sun", "powerPlantId")
                        )
                        if key in row
                    }
                )
            if len(readings) == total:
                break
            number += 1
        ordered = tuple(sorted(readings, key=lambda reading: reading.timestamp))
        if len({reading.timestamp for reading in ordered}) != len(ordered):
            raise EpiasError("duplicate_epias_timestamps")
        return EpiasDay(
            plant,
            day,
            ordered,
            datetime.now(UTC),
            json.dumps({"items": safe_rows}, ensure_ascii=False, allow_nan=False),
            endpoint=f"{BASE_URL}/{endpoint}",
            source_kind=source_kind,
            end_day=end_day if settlement else None,
        )
