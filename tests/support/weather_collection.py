from datetime import UTC, datetime, timedelta

from django.db import connection

from pyracast.weather import OpenMeteoProvider, WeatherData, WeatherError, normalize_weather
from tests.support.domain import NOW


class WeatherFixture(OpenMeteoProvider):
    def __init__(self, *, fail: bool = False) -> None:
        super().__init__()
        self.calls = 0
        self.fail = fail

    def fetch(
        self, latitude: float, longitude: float, start: datetime, end: datetime
    ) -> WeatherData:
        assert not connection.in_atomic_block
        self.calls += 1
        if self.fail:
            raise WeatherError("weather_request_failed")
        first = start.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
        stamps = [first - timedelta(hours=1)]
        while stamps[-1] < end:
            stamps.append(stamps[-1] + timedelta(hours=1))
        return normalize_weather(
            {
                "latitude": latitude,
                "longitude": longitude,
                "utc_offset_seconds": 0,
                "hourly_units": {
                    "time": "unixtime",
                    "temperature_2m": "°C",
                    "snow_depth": "m",
                    "snowfall": "cm",
                    "shortwave_radiation": "W/m²",
                },
                "hourly": {
                    "time": [int(stamp.timestamp()) for stamp in stamps],
                    "temperature_2m": [10] * len(stamps),
                    "snow_depth": [0] * len(stamps),
                    "snowfall": [0] * len(stamps),
                    "shortwave_radiation": [600] * len(stamps),
                },
            },
            latitude=latitude,
            longitude=longitude,
            start=start,
            end=end,
            collected_at=NOW,
        )
