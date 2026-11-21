import io
import re
import runpy
import socket
import uuid
from collections.abc import Callable, Iterator
from contextlib import redirect_stdout
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from math import cos, radians
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
import pytest
from astral import Observer, sun
from django.conf import settings
from psycopg import sql
from psycopg.types.json import Jsonb

from pyracast.connections.models import SiteBinding
from pyracast.plants.models import PlantConfiguration
from pyracast.plants.numerics import fit_calibration
from pyracast.plants.tracking import MODEL_V1, MODEL_V2
from pyracast.plants.transfer import reference_power_mw
from scripts import evaluate_tracker_geometry as evaluation

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = (ROOT / "tests/fixtures/tracker_archive_schema.sql").read_bytes()
ZONE = ZoneInfo("Europe/Istanbul")
LATITUDE, LONGITUDE = 38.123457, 27.654321
FIRST_DAY = date(2026, 10, 1)
DAYS = 50
FACTOR = 0.95
COLLECTED = datetime(2026, 11, 25, tzinfo=UTC)
REGION_ID = 612345
SECRETS = (
    "Secret Sun Farm",
    "Hidden Region",
    "40W0000SECRETEIC",
    "SITE-SECRET-ID",
    "RAW-SECRET-PAYLOAD",
    "PROVIDER-SECRET",
    "SECRET-TIMESTAMP",
    "SECRET-READINGS",
    "SECRET-START",
    "SECRET-INTERVALS",
    "38.123457",
    "27.654321",
    "38.123",
    "27.654",
)
IDS = {
    "v1": (734211, 845311, 956411),
    "v2": (734219, 845319, 956419),
    "fixed": (734230, 845330, 956430),
    "unmodeled": (734240, 845340, 956440),
    "broken": (734250, 845350, 956450),
    "disabled": (734260, 845360, 956460),
    "generation": (734270, 845370, 956470),
    "sparse": (734280, 845380, 956480),
}
REVISION_ID = 845329
EIC = "40W0000SECRETEIC"
PROVIDER_IDS = {"realtime_generation": 987654, "settlement_injection": 987655}
OTHER_IDS = (REGION_ID, REVISION_ID, 987654, 987655, 991001, 7_000_001)

type Database = psycopg.Connection[Any]


def server_parameters(database: str) -> dict[str, Any]:
    default = settings.DATABASES["default"]
    return {
        "host": default["HOST"],
        "port": default["PORT"],
        "user": default["USER"],
        "password": default["PASSWORD"],
        "dbname": database,
    }


def environment(database: str) -> dict[str, str]:
    parameters = server_parameters(database)
    return {
        "PYRACAST_ARCHIVE_HOST": str(parameters["host"] or "127.0.0.1"),
        "PYRACAST_ARCHIVE_PORT": str(parameters["port"] or "5432"),
        "PYRACAST_ARCHIVE_USER": str(parameters["user"]),
        "PYRACAST_ARCHIVE_PASSWORD": str(parameters["password"] or ""),
        "PYRACAST_ARCHIVE_DATABASE": database,
    }


def _database(fill: Callable[[Database], None]) -> Iterator[str]:
    name = f"pyracast_tracker_archive_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(**server_parameters("postgres"), autocommit=True) as server:
        server.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        with psycopg.connect(**server_parameters(name)) as connection:
            connection.execute(SCHEMA)
            fill(connection)
        yield name
    finally:
        with psycopg.connect(**server_parameters("postgres"), autocommit=True) as server:
            server.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def _digest(database: str) -> list[tuple[str, Any]]:
    with psycopg.connect(**server_parameters(database)) as connection:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1"
            )
        ]
        return [
            (
                table,
                connection.execute(
                    sql.SQL(
                        "SELECT count(*), md5(coalesce(string_agg(t::text, '|' ORDER BY t::text),"
                        " '')) FROM {} AS t"
                    ).format(sql.Identifier(table))
                ).fetchone(),
            )
            for table in tables
        ]


def _insert(connection: Database, table: str, values: dict[str, Any]) -> None:
    connection.execute(
        sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
            sql.Identifier(table),
            sql.SQL(", ").join(sql.Identifier(column) for column in values),
            sql.SQL(", ").join(sql.Placeholder() for _ in values),
        ),
        list(values.values()),
    )


@dataclass
class Plant:
    key: str
    mounting: str = "tracking"
    model: str = MODEL_V1
    dc_capacity_kw: float | None = 1000.0
    timezone: str = "Europe/Istanbul"
    basis: str = "net_export"
    enabled: bool = True
    hours: dict[datetime, float] = field(default_factory=dict)

    @property
    def ids(self) -> tuple[int, int, int]:
        return IDS[self.key]


class Archive:
    def __init__(self, connection: Database) -> None:
        self.connection = connection
        self.next_id = OTHER_IDS[-1] - 1

    def identifier(self) -> int:
        self.next_id += 1
        return self.next_id

    def plant(self, plant: Plant) -> None:
        plant_id, configuration_id, binding_id = plant.ids
        _insert(
            self.connection,
            "connections_sitebinding",
            {
                "id": binding_id,
                "region_revision": 1,
                "site_id": "SITE-SECRET-ID",
                "capacity_kw": 1000.0,
                "power_basis": "ac",
                "capacity_semantics_verified": True,
                "tilt_degrees": 0.0,
                "azimuth_degrees": 0.0,
                "losses_percent": 0.0,
                "latitude": LATITUDE,
                "longitude": LONGITUDE,
                "timezone": plant.timezone,
                "health": "healthy",
                "created_at": COLLECTED,
                "provider_account_id": 1,
                "region_id": REGION_ID,
                "settings_verification_reference": "PROVIDER-SECRET",
                "dc_capacity_kw": plant.dc_capacity_kw,
            },
        )
        _insert(
            self.connection,
            "plants_plant",
            {
                "id": plant_id,
                "name": "Secret Sun Farm",
                "created_at": COLLECTED,
                "region_id": REGION_ID,
                "current_configuration_id": configuration_id,
                "automatic_calibration_enabled": False,
            },
        )
        self.configuration(plant, configuration_id, 1)
        _insert(
            self.connection,
            "metering_epiasconnection",
            {
                "id": plant_id,
                "provider_plant_id": PROVIDER_IDS["realtime_generation"],
                "eic": EIC,
                "name": "Secret Sun Farm",
                "enabled": plant.enabled,
                "revision": 1,
                "created_at": COLLECTED,
                "updated_at": COLLECTED,
                "connected_by_id": 1,
                "plant_id": plant_id,
                "settlement_plant_id": PROVIDER_IDS["settlement_injection"],
            },
        )

    def configuration(self, plant: Plant, identifier: int, revision: int) -> None:
        plant_id, _, binding_id = plant.ids
        _insert(
            self.connection,
            "plants_plantconfiguration",
            {
                "id": identifier,
                "revision": revision,
                "dc_capacity_mw": 130.0,
                "measurement_basis": plant.basis,
                "created_at": COLLECTED,
                "binding_id": binding_id,
                "created_by_id": 1,
                "plant_id": plant_id,
                "mounting_type": plant.mounting,
                "region_id": REGION_ID,
            },
        )

    def reference(self, plant: Plant, rows: object) -> None:
        identifier = self.identifier()
        _insert(
            self.connection,
            "forecasts_forecastsnapshot",
            {
                "id": identifier,
                "raw_payload": Jsonb({"marker": "RAW-SECRET-PAYLOAD"}),
                "normalized_intervals": Jsonb(rows),
                "collected_at": COLLECTED + timedelta(seconds=identifier - OTHER_IDS[-1]),
                "job_id": identifier,
                "source_kind": "estimated_actuals",
                "weather_error": "",
                "binding_id": plant.ids[2],
            },
        )

    def production(
        self,
        plant: Plant,
        day: date,
        kind: str,
        readings: object,
        *,
        collected: datetime = COLLECTED,
        provider: dict[str, object] | None = None,
        policy: str = "hour-start-istanbul-v1",
    ) -> None:
        plant_id, configuration_id, _ = plant.ids
        identifier = self.identifier()
        _insert(
            self.connection,
            "metering_epiassnapshot",
            {
                "id": identifier,
                "attempt": 1,
                "target_day": day,
                "collected_at": collected,
                "available_at": collected,
                "raw_payload": Jsonb({"marker": "RAW-SECRET-PAYLOAD"}),
                "readings": Jsonb(readings),
                "content_sha256": "a" * 64,
                "complete": True,
                "configuration_id": configuration_id,
                "connection_id": plant_id,
                "job_id": identifier,
                "endpoint": "PROVIDER-SECRET",
                "normalization_policy": policy,
                "provider_plant": Jsonb(provider or {"id": PROVIDER_IDS[kind], "eic": EIC}),
                "source_kind": kind,
            },
        )


def _half_hours(day: date) -> list[tuple[datetime, datetime]]:
    start = datetime.combine(day, datetime.min.time(), ZONE).astimezone(UTC)
    return [
        (start + timedelta(minutes=30 * step), start + timedelta(minutes=30 * step + 30))
        for step in range(48)
    ]


def _reference_kw(start: datetime, day_index: int) -> float:
    zenith = sun.zenith(Observer(LATITUDE, LONGITUDE, 0.0), start + timedelta(minutes=15))
    clearness = 0.55 + 0.4 * ((day_index * 7) % 11) / 10
    return round(float(850 * clearness * max(0.0, cos(radians(zenith))) ** 1.2), 6)


def _reading(start: datetime, energy: str | None) -> dict[str, object]:
    local = start.astimezone(ZONE)
    return {
        "provider_timestamp": local.isoformat(),
        "hour": f"{local.hour:02}:00",
        "period_start": local.isoformat(),
        "period_end": (local + timedelta(hours=1)).isoformat(),
        "total_mwh": energy,
        "sun_mwh": None,
    }


def _day_hours(plant: Plant, day: date) -> list[datetime]:
    return sorted(hour for hour in plant.hours if hour.astimezone(ZONE).date() == day)


def _generate(archive: Archive, plant: Plant, days: range | tuple[int, ...] = range(DAYS)) -> None:
    configuration = PlantConfiguration(
        binding=SiteBinding(
            capacity_kw=1000.0,
            dc_capacity_kw=plant.dc_capacity_kw,
            losses_percent=0.0,
            latitude=LATITUDE,
            longitude=LONGITUDE,
            timezone=plant.timezone,
        ),
        dc_capacity_mw=130.0,
        mounting_type=plant.mounting,
    )
    for index in days:
        day = FIRST_DAY + timedelta(days=index)
        intervals = [(start, end, _reference_kw(start, index)) for start, end in _half_hours(day)]
        archive.reference(
            plant,
            [
                {"start": start.isoformat(), "end": end.isoformat(), "power_kw": power}
                for start, end, power in intervals
            ],
        )
        hourly: dict[datetime, float] = {}
        for start, end, power in intervals:
            transferred = reference_power_mw(
                configuration, start, end, power, model_version=plant.model
            )
            hour = start.replace(minute=0)
            hourly[hour] = hourly.get(hour, 0.0) + FACTOR * transferred / 2
        plant.hours.update(hourly)
        kind = "settlement_injection" if index % 2 else "realtime_generation"
        archive.production(
            plant,
            day,
            kind,
            [_reading(hour, f"{energy:.9f}") for hour, energy in sorted(hourly.items())],
        )
        stale = [_reading(hour, "999") for hour in sorted(hourly)]
        archive.production(plant, day, kind, stale, collected=COLLECTED - timedelta(days=1))
        if index % 2:
            archive.production(plant, day, "realtime_generation", stale)


def _malformed(archive: Archive, plant: Plant) -> None:
    later = COLLECTED + timedelta(days=1)
    unreadable = FIRST_DAY + timedelta(days=DAYS - 4)
    archive.production(
        plant, unreadable, "realtime_generation", {"note": "SECRET-READINGS"}, collected=later
    )
    damaged = FIRST_DAY + timedelta(days=DAYS - 6)
    hours = _day_hours(plant, damaged)
    noon = hours[12]
    readings: list[object] = [
        _reading(hour, f"{plant.hours[hour]:.9f}") for hour in hours if hour != noon
    ]
    readings += [
        "SECRET-READINGS",
        {**_reading(noon, "1"), "period_start": "SECRET-TIMESTAMP"},
        _reading(noon, "SECRET-READINGS"),
        _reading(noon, "NaN"),
        _reading(noon, None),
        {**_reading(noon, "1"), "period_end": (noon + timedelta(minutes=30)).isoformat()},
    ]
    archive.production(plant, damaged, "realtime_generation", readings, collected=later)
    archive.reference(plant, "SECRET-INTERVALS")
    archive.reference(
        plant,
        [
            {"start": "SECRET-START", "end": hours[1].isoformat(), "power_kw": 1},
            "SECRET-INTERVALS",
            {"start": hours[0].isoformat(), "end": hours[1].isoformat(), "power_kw": "NaN"},
        ],
    )
    manual = _day_hours(plant, FIRST_DAY + timedelta(days=30))[11]
    _insert(
        archive.connection,
        "plants_productionobservation",
        {
            "id": 991001,
            "starts_at": manual,
            "ends_at": manual + timedelta(minutes=30),
            "energy_mwh": 1.0,
            "quality": "valid",
            "source": "PROVIDER-SECRET",
            "recorded_at": COLLECTED,
            "configuration_id": plant.ids[1],
            "plant_id": plant.ids[0],
            "recorded_by_id": 1,
            "is_current": True,
            "is_reverted": False,
        },
    )


def _populate(connection: Database) -> None:
    _insert(
        connection,
        "regions_region",
        {
            "id": REGION_ID,
            "name": "Hidden Region",
            "latitude": LATITUDE,
            "longitude": LONGITUDE,
            "timezone": "Europe/Istanbul",
            "revision": 1,
            "status": "approved",
            "created_at": COLLECTED,
            "tenant_id": 1,
        },
    )
    archive = Archive(connection)
    by_v1, by_v2 = Plant("v1", model=MODEL_V1), Plant("v2", model=MODEL_V2)
    broken = Plant("broken", timezone="Mars/Olympus")
    for plant in (
        by_v1,
        by_v2,
        Plant("fixed", mounting="fixed"),
        Plant("unmodeled", dc_capacity_kw=None),
        broken,
    ):
        archive.plant(plant)
    archive.configuration(by_v2, REVISION_ID, 2)
    _generate(archive, by_v1)
    _generate(archive, by_v2)
    _malformed(archive, by_v2)
    archive.production(broken, FIRST_DAY, "realtime_generation", [])


def _local_hours(day: date) -> list[datetime]:
    return [datetime.combine(day, time(hour), ZONE) for hour in range(24)]


def _populate_selection(connection: Database) -> None:
    archive = Archive(connection)
    connected, disabled = Plant("v1"), Plant("disabled", enabled=False)
    generation, sparse = Plant("generation", basis="ac_generation"), Plant("sparse")
    for plant in (connected, disabled, generation, sparse):
        archive.plant(plant)
    hours = _local_hours(FIRST_DAY)
    whole_day = [_reading(hour, "1.000000000") for hour in hours]
    for plant in (connected, disabled, generation):
        archive.production(plant, FIRST_DAY, "realtime_generation", whole_day)
    later = COLLECTED + timedelta(days=1)
    noon = [_reading(hours[12], "999")]
    for kind, provider in (
        ("realtime_generation", {"id": 111, "eic": EIC}),
        ("realtime_generation", {"id": PROVIDER_IDS["realtime_generation"], "eic": "OTHER"}),
        ("settlement_injection", {"id": PROVIDER_IDS["realtime_generation"], "eic": EIC}),
    ):
        archive.production(connected, FIRST_DAY, kind, noon, collected=later, provider=provider)
    archive.production(
        connected,
        FIRST_DAY + timedelta(days=1),
        "realtime_generation",
        [_reading(hour, "1") for hour in _local_hours(FIRST_DAY + timedelta(days=1))],
        policy="hour-start-utc-v0",
    )
    _generate(archive, sparse, (0, 7))


@pytest.fixture(scope="module")
def archive() -> Iterator[str]:
    yield from _database(_populate)


@pytest.fixture(scope="module")
def selection_archive() -> Iterator[str]:
    yield from _database(_populate_selection)


@pytest.fixture
def empty_archive() -> Iterator[str]:
    yield from _database(lambda _: None)


@dataclass(frozen=True)
class Run:
    code: int
    output: str
    network: list[object]
    fits: list[tuple[tuple[object, ...], tuple[object, ...]]]
    unchanged: bool


@pytest.fixture(scope="module")
def run(archive: str) -> Run:
    network: list[object] = []
    fits: list[tuple[tuple[object, ...], tuple[object, ...]]] = []
    original = fit_calibration

    def refuse(*args: object, **kwargs: object) -> None:
        network.append(args)
        raise OSError

    def recorded(samples: Any, **options: Any) -> Any:
        inputs = tuple(
            (
                sample.start,
                sample.end,
                sample.energy_mwh,
                tuple((piece.start, piece.end) for piece in sample.intervals),
            )
            for sample in samples
        )
        powers = tuple(piece.power_mw for sample in samples for piece in sample.intervals)
        fits.append(((inputs, tuple(sorted(options.items()))), powers))
        return original(samples, **options)

    before = _digest(archive)
    output = io.StringIO()
    with pytest.MonkeyPatch.context() as patch, redirect_stdout(output):
        patch.setattr(socket.socket, "connect", refuse)
        patch.setattr(socket, "create_connection", refuse)
        patch.setattr("scripts.evaluate_tracker_geometry.fit_calibration", recorded)
        code = evaluation.main(environment(archive))
    return Run(code, output.getvalue(), network, fits, _digest(archive) == before)


def _section(output: str, ordinal: int) -> str:
    match = re.search(rf"^plant {ordinal}\n((?:  .*\n)*)", output, re.MULTILINE)
    assert match, output
    return match.group(1)


def _version(section: str, label: str) -> tuple[float, float]:
    match = re.search(
        rf"^  {label}: MAE \S+ MWh \(\S+ %\), RMSE (\S+) MWh .* median factor (\S+)$",
        section,
        re.MULTILINE,
    )
    assert match, section
    return float(match.group(1)), float(match.group(2))


def _skipped(section: str) -> dict[str, int]:
    line = re.search(r"^  skipped: (.*)$", section, re.MULTILINE)
    assert line, section
    return {
        reason: int(count)
        for reason, count in re.findall(r"([a-z][a-zA-Z :-]*?) (\d+)(?:, |$)", line.group(1))
    }


def _interval(section: str) -> tuple[float, float, float]:
    match = re.search(
        r"^  RMSE v2 - v1: (\S+) MWh, 95 % interval \[(\S+), (\S+)\]$", section, re.MULTILINE
    )
    assert match, section
    difference, low, high = (float(value) for value in match.groups())
    return difference, low, high


def test_the_evaluation_prefers_the_version_that_generated_production(run: Run) -> None:
    assert run.code == 0
    generated_by_v1, generated_by_v2 = _section(run.output, 1), _section(run.output, 2)
    (v1_error, v1_factor), (v2_error, _) = (
        _version(generated_by_v1, "v1"),
        _version(generated_by_v1, "v2"),
    )
    assert v1_error < v2_error
    assert v1_factor == pytest.approx(FACTOR, abs=0.005)
    difference, low, _ = _interval(generated_by_v1)
    assert difference > 0
    assert low > 0
    (v1_error, _), (v2_error, v2_factor) = (
        _version(generated_by_v2, "v1"),
        _version(generated_by_v2, "v2"),
    )
    assert v2_error < v1_error
    assert v2_factor == pytest.approx(FACTOR, abs=0.005)
    difference, _, high = _interval(generated_by_v2)
    assert difference < 0
    assert high < 0
    assert "  decision: no" in generated_by_v1
    assert "  decision: yes" in generated_by_v2
    assert re.search(
        r"^  days: complete \d+, evaluated \d+; weeks: scored \d+, skipped 0$",
        generated_by_v1,
        re.M,
    )
    assert run.output.splitlines()[-1] == "v2 not worse: no"


def test_plants_without_tracker_geometry_are_reported_and_failures_stay_per_plant(
    run: Run,
) -> None:
    lines = run.output.splitlines()
    assert lines[:2] == ["tracker geometry evaluation (read-only)", evaluation.RULE]
    assert "plant 3: geometry not modeled" in lines
    assert "plant 4: failed: ZoneInfoNotFoundError" in lines
    assert not any(line.startswith("plant 5") for line in lines)


def test_the_evaluation_counts_malformed_rows_without_showing_them(run: Run) -> None:
    skipped = _skipped(_section(run.output, 2))
    assert skipped["readings: TypeError"] == 1
    assert skipped["reading: TypeError"] == 1
    assert skipped["reading: ValueError"] == 2
    assert skipped["invalid reading"] == 2
    assert skipped["unreported reading"] == 1
    assert skipped["reference intervals: TypeError"] == 1
    assert skipped["reference interval: ValueError"] == 1
    assert skipped["reference interval: TypeError"] == 1
    assert skipped["invalid reference interval"] == 1
    assert skipped["manual override"] == 1
    assert skipped["incomplete day"] > 0
    assert skipped["outside daylight"] > 0
    assert skipped["fit-only first week"] > 0
    assert set(_skipped(_section(run.output, 1))) == {"fit-only first week", "outside daylight"}
    assert "Traceback" not in run.output


def _evaluate(database: str, **patches: object) -> tuple[int, str]:
    output = io.StringIO()
    with pytest.MonkeyPatch.context() as patch, redirect_stdout(output):
        for name, value in patches.items():
            patch.setattr(f"scripts.evaluate_tracker_geometry.{name}", value)
        code = evaluation.main(environment(database))
    return code, output.getvalue()


@pytest.fixture(scope="module")
def selection(selection_archive: str) -> str:
    code, output = _evaluate(selection_archive)
    assert code == 0
    return output


def test_production_comes_only_from_the_enabled_connection_and_its_provider_plants(
    selection: str,
) -> None:
    connected = _skipped(_section(selection, 1))
    assert connected.pop("unsupported normalization policy") == 1
    assert set(connected) <= {"outside daylight", "reference missing"}
    assert sum(connected.values()) == 24
    assert "  skipped: none" in _section(selection, 2)


def test_unusable_measurements_and_fits_are_counted_by_reason(
    selection_archive: str, selection: str
) -> None:
    assert _skipped(_section(selection, 3)) == {"measurement basis not net export": 1}
    sparse = _section(selection, 4)
    assert "  days: complete 2, evaluated 0; weeks: scored 0, skipped 1" in sparse
    assert "  decision: no" in sparse
    insufficient = _skipped(sparse)["fit insufficient"]
    assert insufficient > 0

    def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError

    code, output = _evaluate(selection_archive, fit_calibration=fail)
    assert code == 0
    assert _skipped(_section(output, 4))["fit failed: RuntimeError"] == insufficient
    assert "Traceback" not in output


def test_the_output_holds_no_identifying_value(run: Run) -> None:
    for value in SECRETS:
        assert value not in run.output
    for identifier in (*(value for ids in IDS.values() for value in ids), *OTHER_IDS):
        assert str(identifier) not in run.output


def test_the_evaluation_refits_each_version_on_identical_inputs(run: Run) -> None:
    assert len(run.fits) >= 16
    assert len(run.fits) % 2 == 0
    pairs = list(zip(run.fits[::2], run.fits[1::2], strict=True))
    assert all(first[0] == second[0] for first, second in pairs)
    assert all(first[1] != second[1] for first, second in pairs)


def test_the_evaluation_makes_no_network_call_and_writes_nothing(run: Run) -> None:
    assert run.code == 0
    assert run.network == []
    assert run.unchanged


def test_the_session_is_read_only(archive: str) -> None:
    connection = evaluation.connect(environment(archive))
    try:
        assert connection.execute("SHOW default_transaction_read_only").fetchone() == ("on",)
        assert connection.read_only is True
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            connection.execute("DELETE FROM plants_plant")
    finally:
        connection.close()


def test_a_session_that_is_not_read_only_is_refused(
    archive: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    connect = psycopg.connect

    def writable(*args: Any, **kwargs: Any) -> Any:
        del kwargs["options"]
        return connect(*args, **kwargs)

    monkeypatch.setattr(psycopg, "connect", writable)
    assert evaluation.main(environment(archive)) == 1
    assert capsys.readouterr().out == "evaluation failed: ReadOnlyRefusedError\n"


def test_an_archive_without_tracking_plants_decides_no(
    empty_archive: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert evaluation.main(environment(empty_archive)) == 0
    assert capsys.readouterr().out.splitlines()[-2:] == ["no tracking plant", "v2 not worse: no"]


def test_connection_settings_are_required_and_failures_print_only_their_class(
    capsys: pytest.CaptureFixture[str],
) -> None:
    incomplete = environment("unused")
    del incomplete["PYRACAST_ARCHIVE_DATABASE"]
    assert evaluation.main(incomplete) == 2
    assert capsys.readouterr().out == "missing environment variable PYRACAST_ARCHIVE_DATABASE\n"
    absent = f"pyracast_absent_{uuid.uuid4().hex[:8]}"
    assert evaluation.main(environment(absent)) == 1
    assert capsys.readouterr().out == "evaluation failed: OperationalError\n"


def test_the_command_line_reads_its_settings_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in environment("unused"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PYRACAST_ARCHIVE_USER", "reader")
    with pytest.raises(SystemExit) as exit_status:
        runpy.run_path(str(ROOT / "scripts/evaluate_tracker_geometry.py"), run_name="__main__")
    assert exit_status.value.code == 2
    assert capsys.readouterr().out == "missing environment variable PYRACAST_ARCHIVE_DATABASE\n"


@pytest.mark.parametrize(
    ("weeks", "decision"),
    [
        ([(1.0, 1.0, 10)] * 4, True),
        ([(1.0, 2.0, 10), (1.0, 2.1, 10), (1.0, 1.9, 10), (1.0, 2.0, 10)], False),
        ([(2.0, 1.0, 10), (2.1, 1.0, 10), (1.9, 1.0, 10), (2.0, 1.0, 10)], True),
        ([(1.0, 2.0, 10)] * 3, False),
    ],
)
def test_the_decision_rule_uses_the_jackknife_interval_of_the_rmse_difference(
    weeks: list[tuple[float, float, int]], decision: bool
) -> None:
    report = evaluation.PlantReport(1, weeks=[evaluation.WeekErrors(*week) for week in weeks])
    assert report.not_worse is decision
    assert evaluation.render([report]).endswith(
        f"  decision: {'yes' if decision else 'no'}\nv2 not worse: {'yes' if decision else 'no'}\n"
    )


def test_the_rmse_difference_and_its_interval() -> None:
    same = [evaluation.WeekErrors(4.0, 9.0, 4), evaluation.WeekErrors(4.0, 9.0, 4)]
    assert evaluation.rmse_difference(same) == (0.5, 0.5, 0.5)
    spread = evaluation.rmse_difference(
        [evaluation.WeekErrors(1.0, 4.0, 1), evaluation.WeekErrors(4.0, 4.0, 1)]
        + [evaluation.WeekErrors(1.0, 1.0, 1)] * 2
    )
    assert spread[1] < spread[0] < spread[2]


def test_the_evaluation_transfer_equals_the_application_transfer() -> None:
    configuration = evaluation.Configuration(
        id=1,
        plant_id=1,
        dc_capacity_mw=130.0,
        limit_mw=None,
        net_export=True,
        binding_id=1,
        capacity_kw=1000.0,
        dc_capacity_kw=1300.0,
        losses_percent=7.5,
        latitude=LATITUDE,
        longitude=LONGITUDE,
        timezone="Europe/Istanbul",
    )
    application = PlantConfiguration(
        binding=SiteBinding(
            capacity_kw=1000.0,
            dc_capacity_kw=1300.0,
            losses_percent=7.5,
            latitude=LATITUDE,
            longitude=LONGITUDE,
            timezone="Europe/Istanbul",
        ),
        dc_capacity_mw=130.0,
        mounting_type="tracking",
    )
    for start, end in _half_hours(date(2026, 6, 21))[8:40]:
        for model in (MODEL_V1, MODEL_V2):
            assert evaluation.transferred_mw(configuration, (start, end, 431.5), model) == (
                reference_power_mw(application, start, end, 431.5, model_version=model)
            )
