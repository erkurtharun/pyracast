import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from threading import Barrier
from typing import Any, cast

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import close_old_connections, connection

from pyracast.connections.models import SiteBinding
from pyracast.forecasts.actuals import select_archived_actual_source
from pyracast.forecasts.coverage import history_day_complete
from pyracast.forecasts.historical_upload import (
    import_historical_upload,
    parse_historical_upload,
    preview_historical_upload,
)
from pyracast.forecasts.models import CollectionJob, ForecastSnapshot, HistoricalReferenceImport
from pyracast.irradiance import local_day_bounds
from pyracast.plants.calibration_data import build_calibration_dataset
from pyracast.plants.models import CalibrationRun, Plant
from pyracast.plants.numerics import evaluate_samples, fit_calibration
from pyracast.plants.operations import create_plant
from pyracast.plants.production_changes import ObservationInput, import_observations
from pyracast.regions.models import AuditEvent
from tests.support.domain import NOW, System
from tests.support.historical_reference_import import system_for_import


def row(**updates: Any) -> dict[str, Any]:
    return {
        "period_end": "2026-11-07T12:30:00+03:00",
        "period": "PT30M",
        "pv_power_rooftop": 0.5,
    } | updates


def document(rows: object | None = None, **metadata: Any) -> bytes:
    return json.dumps({"estimated_actuals": [row()] if rows is None else rows} | metadata).encode()


def upload(
    system: System, content: bytes | None = None, **options: Any
) -> HistoricalReferenceImport:
    return import_historical_upload(
        system.admin,
        system.tenant.pk,
        system.region.pk,
        system.binding.pk,
        parse_historical_upload(document() if content is None else content, "historical.json"),
        "historical.json",
        settings_confirmed=True,
        **options,
    )[0]


@pytest.mark.parametrize("offset", ["Z", "+00:00", "+03:00", "+02:00"])
def test_offset_timestamps_preserve_instants_and_raw_rows(offset: str) -> None:
    content = document([row(period_end=f"2026-11-07T12:30:00{offset}")])
    parsed = parse_historical_upload(content, "history.json", source_timezone="Europe/Istanbul")
    expected_hour = 9 if offset == "+03:00" else 10 if offset == "+02:00" else 12
    assert parsed.power.intervals[0].end == datetime(2026, 11, 7, expected_hour, 30, tzinfo=UTC)
    assert parsed.power.raw_rows[0]["period_end"] == f"2026-11-07T12:30:00{offset}"
    assert parsed.power.sha256 == sha256(content).hexdigest()
    assert parsed.naive_timezone is None
    assert parsed.warnings == ()


def test_supplied_export_offsets_do_not_shift_summer_intervals_to_local_midnight() -> None:
    parsed = parse_historical_upload(
        document([row(period_end="2026-08-01T00:30:00+02:00")]), "history.json"
    )
    interval = parsed.power.intervals[0]
    assert interval.start == datetime(2026, 7, 31, 22, tzinfo=UTC)
    assert interval.end == datetime(2026, 7, 31, 22, 30, tzinfo=UTC)


def test_upload_limits_deep_json_and_changing_row_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("pyracast.forecasts.historical_upload.MAX_UPLOAD_BYTES", 1)
    with pytest.raises(ValidationError, match="7 MiB"):
        parse_historical_upload(document(), "h.json")
    monkeypatch.setattr("pyracast.forecasts.historical_upload.MAX_UPLOAD_BYTES", 7 * 1024 * 1024)
    monkeypatch.setattr("pyracast.forecasts.historical_upload.MAX_UPLOAD_ROWS", 1)
    with pytest.raises(ValidationError, match="200000"):
        parse_historical_upload(document([row(), row()]), "h.json")
    monkeypatch.setattr("pyracast.forecasts.historical_upload.MAX_UPLOAD_ROWS", 200_000)
    depth = 100
    with pytest.raises(ValidationError, match="nested too deeply"):
        parse_historical_upload(b"[" * depth + b"0" + b"]" * depth, "h.json")
    with pytest.raises(ValidationError, match="settings change"):
        parse_historical_upload(
            document([row(latitude=40), row(period_end="2026-11-08T12:30:00Z", latitude=41)]),
            "h.json",
        )


def test_decoder_recursion_exhaustion_is_a_validation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def exhausted(*args: Any, **kwargs: Any) -> object:
        raise RecursionError

    monkeypatch.setattr("pyracast.irradiance.solcast_export.json.loads", exhausted)
    with pytest.raises(ValidationError, match="nested too deeply"):
        parse_historical_upload(b"{}", "h.json")


def test_metadata_duration_and_csv_source_zone_preserve_file_evidence() -> None:
    parsed = parse_historical_upload(
        document(
            [{"period_end": "2026-11-07T12:30:00Z", "pv_power_rooftop": 0.5}],
            metadata={"period": "PT60M"},
        ),
        "h.json",
    )
    assert parsed.period == "PT60M"
    assert not parsed.period_declared
    parsed = parse_historical_upload(
        b"period_end,period,pv_power_rooftop,TimeZone\n2026-11-07T12:30:00,PT30M,0.5,UTC\n",
        "h.csv",
    )
    assert parsed.naive_timezone == "UTC"
    assert parsed.power.intervals[0].end == datetime(2026, 11, 7, 12, 30, tzinfo=UTC)


def test_naive_timestamp_requires_explicit_source_zone_and_rejects_dst_ambiguity() -> None:
    content = document([row(period_end="2026-11-07T12:30:00")])
    with pytest.raises(ValidationError, match="explicit source timezone"):
        parse_historical_upload(content, "history.json")
    parsed = parse_historical_upload(content, "history.json", source_timezone="Europe/Istanbul")
    assert parsed.power.intervals[0].end == datetime(2026, 11, 7, 9, 30, tzinfo=UTC)
    assert parsed.naive_timezone == "Europe/Istanbul"
    assert len(parsed.warnings) == 1
    for timestamp in ("2026-03-29T02:30:00", "2026-10-25T02:30:00"):
        with pytest.raises(ValidationError, match="Ambiguous or nonexistent"):
            parse_historical_upload(
                document([row(period_end=timestamp)]),
                "history.json",
                source_timezone="Europe/Berlin",
            )


def test_file_timezone_is_source_representation_and_can_disambiguate_naive_timestamps() -> None:
    parsed = parse_historical_upload(
        document([row(period_end="2026-11-07T12:30:00")], metadata={"time_zone": "UTC"}),
        "history.json",
    )
    assert parsed.naive_timezone == "UTC"
    assert parsed.power.intervals[0].end == datetime(2026, 11, 7, 12, 30, tzinfo=UTC)
    with pytest.raises(ValidationError, match="conflicts"):
        parse_historical_upload(
            document([row(period_end="2026-11-07T12:30:00")], timezone="UTC"),
            "history.json",
            source_timezone="Europe/Istanbul",
        )
    parsed = parse_historical_upload(document(timezone="uninterpretable"), "history.json")
    assert parsed.naive_timezone is None
    with pytest.raises(ValidationError, match="conflicts"):
        parse_historical_upload(
            document([row(period_start="2026-11-07T12:00:00")], timezone="UTC"),
            "history.json",
            source_timezone="Europe/Istanbul",
        )


@pytest.mark.parametrize("period", ["PT30M", "PT60M"])
def test_csv_saved_hobby_power_and_duration(period: str) -> None:
    content = (
        f"PeriodEnd,Period,PvEstimate,Extra\n2026-11-07T12:30:00Z,{period},0.5,kept\n".encode()
    )
    parsed = parse_historical_upload(content, "export.CSV")
    assert parsed.format == "csv"
    assert parsed.source_product == "rooftop_pv_power"
    assert parsed.period == period
    assert parsed.power.intervals[0].power_kw == 0.5
    assert parsed.power.raw_rows[0]["Extra"] == "kept"
    assert (parsed.power.intervals[0].end - parsed.power.intervals[0].start).total_seconds() == (
        1800 if period == "PT30M" else 3600
    )


def test_missing_period_requires_selection_or_explicit_bounds() -> None:
    content = b"period_end,pv_power_rooftop\n2026-11-07T12:30:00Z,0.5\n"
    with pytest.raises(ValidationError, match="explicit PT30M or PT60M"):
        parse_historical_upload(content, "history.csv")
    selected = parse_historical_upload(content, "history.csv", period="PT60M")
    assert selected.period_declared
    assert selected.period == "PT60M"
    bounded = parse_historical_upload(
        b"period_start,period_end,pv_power_rooftop\n2026-11-07T12:00:00Z,2026-11-07T12:30:00Z,0.5\n",
        "history.csv",
    )
    assert bounded.period == "PT30M"
    assert not bounded.period_declared
    assert bounded.warnings == ()


@pytest.mark.parametrize(
    ("content", "filename", "options"),
    [
        (b"", "h.json", {}),
        (b"x", "h.txt", {}),
        (b"\xff", "h.csv", {}),
        (b"not json", "h.json", {}),
        (b"[]", "h.json", {}),
        (b'{"forecasts": [], "estimated_actuals": []}', "h.json", {}),
        (document([]), "h.json", {}),
        (document([1]), "h.json", {}),
        (document([row(pv_power_rooftop=True)]), "h.json", {}),
        (document([row(pv_power_rooftop=None)]), "h.json", {}),
        (document([row(pv_power_rooftop=-1)]), "h.json", {}),
        (document([row(pv_power_rooftop="inf")]), "h.json", {}),
        (document([row(pv_power_rooftop="invalid")]), "h.json", {}),
        (document([row(pv_power_rooftop=float("nan"))]), "h.json", {}),
        (b'{"estimated_actuals": [], "arbitrary": 1e999}', "h.json", {}),
        (document([row(pv_power_rooftop=10**400)]), "h.json", {}),
        (document([{"period_end": "2026-11-07T12:30:00Z", "ghi": 100}]), "h.json", {}),
        (document([row(pv_estimate=0.1)]), "h.json", {}),
        (document([row(period_end=None)]), "h.json", {}),
        (document([row(period_end="invalid")]), "h.json", {}),
        (document([row(period_end="2026-11-07")]), "h.json", {}),
        (document([row(period="PT15M")]), "h.json", {}),
        (document([row(period=[])]), "h.json", {}),
        (
            document([row(period_end="2026-11-07T12:30:00", period_start="2026-11-07T10:00:00Z")]),
            "h.json",
            {"source_timezone": "UTC"},
        ),
        (document([row(period_end="9999-01-01T12:30:00Z")]), "h.json", {}),
        (document([row(), row()]), "h.json", {}),
        (document([row(), row(period_end="2026-11-08T12:30:00Z", period="PT60M")]), "h.json", {}),
        (
            document(
                [row(), {"period_end": "2026-11-08T12:30:00Z", "period": "PT30M", "pv_estimate": 1}]
            ),
            "h.json",
            {},
        ),
        (document([row(period_start="2026-11-07T12:00:00Z")]), "h.json", {}),
        (document(), "h.json", {"source_timezone": "Not/AZone"}),
        (document(), "h.json", {"source_timezone": "/invalid"}),
        (document(metadata=[]), "h.json", {}),
        (document(latitude=40, metadata={"latitude": 41}), "h.json", {}),
        (b'{"estimated_actuals": [], "estimated_actuals": []}', "h.json", {}),
        (b"period_end,period_end,pv_estimate\na,b,1\n", "h.csv", {}),
        (b"PeriodEnd,period_end,pv_estimate\na,b,1\n", "h.csv", {}),
        (b"period_end,pv_estimate\na,1,2\n", "h.csv", {}),
        (b"period_end,pv_estimate\na\n", "h.csv", {}),
        (b'period_end,pv_estimate\n"unterminated,1\n', "h.csv", {}),
        (b"", "h.csv", {}),
        (b"\xef\xbb\xbf", "h.csv", {}),
    ],
)
def test_malformed_ambiguous_and_incompatible_uploads_are_rejected(
    content: bytes, filename: str, options: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError):
        parse_historical_upload(content, filename, **options)


@pytest.mark.django_db(transaction=True)
def test_preview_missing_metadata_warns_and_known_conflicts_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = system_for_import(monkeypatch)
    system.binding.refresh_from_db()
    parsed = parse_historical_upload(document(timezone="UTC"), "history.json")
    preview = preview_historical_upload(system.binding, parsed)
    assert preview.interval_count == 1
    assert preview.local_days == (date(2026, 11, 7),)
    assert preview.unit == "kW"
    assert preview.conflicts == ()
    assert preview.verified_fields == ()
    assert "capacity_kw" in preview.missing_fields
    assert preview.warnings
    matched = parse_historical_upload(
        document(metadata={"Capacity": "1", "Tilt": 0, "Azimuth": 0}), "history.json"
    )
    assert set(preview_historical_upload(system.binding, matched).verified_fields) == {
        "capacity_kw",
        "tilt_degrees",
        "azimuth_degrees",
    }
    for key, value in (
        ("capacity_kw", 2),
        ("latitude", "invalid"),
        ("latitude", True),
        ("unit", "kWh"),
        ("site_timezone", "UTC"),
        ("product", "forecasts"),
        ("period", "PT60M"),
    ):
        content = document(metadata={key: value})
        preview = preview_historical_upload(
            system.binding, parse_historical_upload(content, "history.json")
        )
        assert preview.conflicts
        with pytest.raises(ValidationError, match="conflicts"):
            upload(system, content)
    assert not HistoricalReferenceImport.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_file_metadata_verifies_only_present_settings_and_csv_product_is_checked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = system_for_import(monkeypatch)
    system.binding.refresh_from_db()
    metadata = {
        name: getattr(system.binding, name)
        for name in (
            "latitude",
            "longitude",
            "capacity_kw",
            "dc_capacity_kw",
            "azimuth_degrees",
            "losses_percent",
            "site_id",
            "power_basis",
        )
    } | {
        "tilt_degrees": 0,
        "unit": "kW",
        "mounting_type": "fixed",
        "timestamp_convention": "period_end",
        "site_timezone": system.binding.timezone,
        "period": "PT30M",
        "product": "historical_rooftop_pv",
    }
    preview = preview_historical_upload(
        system.binding, parse_historical_upload(document(metadata=metadata), "h.json")
    )
    assert preview.missing_fields == ()
    assert preview.warnings == ()
    assert {"product", "period"} <= set(preview.verified_fields)
    csv = b"period_end,period,pv_power_rooftop,Product\n2026-11-07T12:30:00Z,PT30M,0.5,forecast\n"
    parsed = parse_historical_upload(csv, "h.csv")
    assert preview_historical_upload(system.binding, parsed).conflicts
    with pytest.raises(ValidationError, match="conflicts"):
        import_historical_upload(
            system.admin,
            system.tenant.pk,
            system.region.pk,
            system.binding.pk,
            parsed,
            "h.csv",
            settings_confirmed=True,
        )


@pytest.mark.django_db(transaction=True)
def test_upload_preserves_declared_provenance_availability_and_idempotence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = system_for_import(monkeypatch)
    attempts = system.account.requestattempt_set.count()
    content = document(metadata={"latitude": 40, "unit": "kW"})
    record = upload(system, content)
    assert upload(system, content) == record
    assert record.created_at == NOW
    assert record.content_sha256 == sha256(content).hexdigest()
    assert record.source_settings["settings_matched"]
    provenance = record.source_settings["upload_provenance"]
    assert provenance["file_verified_fields"] == ["latitude", "unit"]
    assert "capacity_kw" in provenance["operator_declared_fields"]
    assert provenance["operator_confirmed"]
    snapshot = ForecastSnapshot.objects.get(job__reference_import=record)
    assert snapshot.source_kind == "estimated_actuals"
    assert snapshot.collected_at == snapshot.available_at == NOW
    assert snapshot.issued_at is None
    assert snapshot.job.endpoint == "historical_import"
    assert snapshot.raw_payload["estimated_actuals"] == [row()]
    assert system.account.requestattempt_set.count() == attempts
    assert AuditEvent.objects.filter(action="historical_reference_imported").count() == 1
    assert upload(system, content, source_reference="Changed declaration") == record


@pytest.mark.django_db(transaction=True)
def test_unknown_dc_capacity_remains_unknown_without_blocking_archive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = system_for_import(monkeypatch)
    SiteBinding.objects.filter(pk=system.binding.pk).update(dc_capacity_kw=None)
    record = upload(system)
    assert record.source_settings["dc_capacity_kw"] is None
    assert record.source_settings["settings_matched"] is False
    assert record.source_settings["upload_provenance"]["binding_dc_capacity_known"] is False
    assert any(
        "DC capacity" in warning
        for warning in record.source_settings["upload_provenance"]["warnings"]
    )
    file_dc = upload(system, document(metadata={"dc_capacity_kw": 1}))
    assert file_dc.source_settings["dc_capacity_kw"] is None
    assert not file_dc.source_settings["settings_matched"]
    assert (
        "dc_capacity_kw" not in file_dc.source_settings["upload_provenance"]["file_verified_fields"]
    )
    for invalid in (True, -1, "invalid", 10**400):
        with pytest.raises(ValidationError, match="conflicts"):
            upload(system, document(metadata={"dc_capacity_kw": invalid}))


@pytest.mark.django_db(transaction=True)
def test_authorization_confirmation_current_configuration_and_binding_are_rechecked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = system_for_import(monkeypatch)
    args = (
        system.admin,
        system.tenant.pk,
        system.region.pk,
        system.binding.pk,
        parse_historical_upload(document(), "history.json"),
        "history.json",
    )
    for options in (
        {"settings_confirmed": False},
        {"settings_confirmed": True, "source_reference": " "},
        {"settings_confirmed": True, "source_reference": "x" * 1001},
    ):
        with pytest.raises(ValidationError, match="Confirm"):
            import_historical_upload(*args, **options)
    with pytest.raises(PermissionDenied):
        import_historical_upload(system.member, *args[1:], settings_confirmed=True)
    plant = Plant.objects.create(region=system.region, name="Configured later")
    with pytest.raises(ValidationError, match="configuration changed"):
        upload(system, expected_configuration_ids=())
    assert upload(system, expected_configuration_ids=((plant.pk, None),))
    SiteBinding.objects.filter(pk=system.binding.pk).update(
        validated_at=None, validated_credential_revision=None
    )
    with pytest.raises(ValidationError, match="has not been validated"):
        upload(system)


@pytest.mark.django_db(transaction=True)
def test_hourly_upload_preserves_native_bounds_and_energy_through_calibration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = system_for_import(monkeypatch)
    plant = create_plant(
        system.member, system.tenant.pk, system.region.pk, name="Hourly reference", dc_capacity_mw=1
    )
    configuration = plant.current_configuration
    assert configuration is not None
    start, _ = local_day_bounds(date(2026, 11, 1), system.binding.timezone)
    starts = [start + timedelta(hours=hour) for hour in range(7 * 24)]
    record = upload(
        system,
        document(
            [
                {
                    "period_end": (instant + timedelta(hours=1)).isoformat(),
                    "period": "PT60M",
                    "pv_power_rooftop": 0.4,
                }
                for instant in starts
            ]
        ),
    )
    snapshots = list(ForecastSnapshot.objects.filter(job__reference_import=record))
    assert len(snapshots) == 7
    for snapshot in snapshots:
        assert len(snapshot.normalized_intervals) == 24
        assert history_day_complete(snapshot, snapshot.job.target_date, system.binding.timezone)
        source = select_archived_actual_source(system.binding, snapshot.job.target_date, NOW)
        assert source is not None
        assert source.snapshot.pk == snapshot.pk
        assert source.composition is None
        assert all(
            datetime.fromisoformat(interval["end"]) - datetime.fromisoformat(interval["start"])
            == timedelta(hours=1)
            for interval in source.intervals
        )
    import_observations(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        configuration_id=configuration.pk,
        unit="MWh",
        observations=[
            ObservationInput(instant, instant + timedelta(hours=1), 0.32) for instant in starts
        ],
    )
    dataset = build_calibration_dataset(CalibrationRun.objects.get(plant=plant), configuration)
    assert len(dataset.samples) == 168
    assert all(
        len(sample.intervals) == 1
        and sample.intervals[0].start == sample.start
        and sample.intervals[0].end == sample.end
        and sample.intervals[0].power_mw == 0.4
        for sample in dataset.samples
    )
    evaluated = evaluate_samples(dataset.samples, timezone=system.binding.timezone, limit_mw=None)
    assert evaluated["valid_days"] == 7
    errors = cast(list[dict[str, object]], evaluated["day_errors"])
    for day in errors:
        assert day["predicted_energy_mwh"] == pytest.approx(9.6)
        assert day["measured_energy_mwh"] == pytest.approx(7.68)
        assert day["error_mwh"] == pytest.approx(1.92)
    fitted = fit_calibration(
        dataset.samples,
        timezone=system.binding.timezone,
        normalization_mw=1,
        limit_mw=None,
        required_windows=dataset.required_windows,
    )
    assert fitted.status == "ready"
    assert fitted.factor is not None
    assert 0.8 < fitted.factor < 0.81
    assert fitted.report["valid_days"] == 7


@pytest.mark.django_db(transaction=True)
def test_concurrent_uploads_publish_one_immutable_dataset(monkeypatch: pytest.MonkeyPatch) -> None:
    system = system_for_import(monkeypatch)
    barrier = Barrier(2)

    def run() -> int:
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return upload(system).pk
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: run(), range(2)))
    assert results[0] == results[1]
    assert HistoricalReferenceImport.objects.count() == 1
    assert CollectionJob.objects.filter(reference_import__isnull=False).count() == 1
