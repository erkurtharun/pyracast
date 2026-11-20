import json
import time
from datetime import datetime, timedelta

import httpx

from pyracast.outbound import TIMEOUT, UnboundedResponseError, read_bounded
from pyracast.weather_data import (
    HOURLY_UNITS,
    WeatherData,
    WeatherError,
    WeatherFailure,
    normalize_weather,
    unsafe_weather_payload,
    weather_coordinate,
    weather_window,
)


class _WeatherKeyTransport(httpx.BaseTransport):
    def __init__(self, transport: httpx.BaseTransport, api_key: str) -> None:
        self.transport = transport
        self.api_key = api_key

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        # Keep the credential off the client request used by HTTPX's request logger.
        wire_request = httpx.Request(
            request.method,
            request.url.copy_add_param("apikey", self.api_key),
            headers=request.headers,
            stream=request.stream,
            extensions=request.extensions,
        )
        return self.transport.handle_request(wire_request)

    def close(self) -> None:
        self.transport.close()


class OpenMeteoProvider:
    def __init__(
        self, transport: httpx.BaseTransport | None = None, api_key: str | None = None
    ) -> None:
        self.transport = transport
        self.api_key = api_key

    def fetch(
        self, latitude: float, longitude: float, start: datetime, end: datetime
    ) -> WeatherData:
        latitude = weather_coordinate(latitude, 90)
        longitude = weather_coordinate(longitude, 180)
        start, end = weather_window(start, end)
        first_instant = start - timedelta(hours=1)
        last_instant = end.replace(minute=0, second=0, microsecond=0)
        if last_instant < end:
            last_instant += timedelta(hours=1)
        params: dict[str, str | float] = {
            "latitude": latitude,
            "longitude": longitude,
            "hourly": ",".join(name for name in HOURLY_UNITS if name != "time"),
            "timeformat": "unixtime",
            "timezone": "UTC",
            "temperature_unit": "celsius",
            "precipitation_unit": "mm",
            "start_date": first_instant.date().isoformat(),
            "end_date": last_instant.date().isoformat(),
        }
        host = "api.open-meteo.com"
        transport = self.transport
        if self.api_key:
            host = "customer-api.open-meteo.com"
            transport = _WeatherKeyTransport(
                transport if transport is not None else httpx.HTTPTransport(), self.api_key
            )
        try:
            started = time.monotonic()
            with (
                httpx.Client(
                    transport=transport,
                    timeout=TIMEOUT,
                    follow_redirects=False,
                ) as client,
                client.stream("GET", f"https://{host}/v1/forecast", params=params) as response,
            ):
                response.raise_for_status()
                payload = json.loads(read_bounded(response, started))
            if unsafe_weather_payload(payload, self.api_key):
                raise WeatherError(WeatherFailure.UNSAFE_RESPONSE)
            return normalize_weather(
                payload, latitude=latitude, longitude=longitude, start=start, end=end
            )
        except httpx.HTTPError, ValueError, RecursionError, UnboundedResponseError:
            raise WeatherError(WeatherFailure.REQUEST_FAILED) from None
