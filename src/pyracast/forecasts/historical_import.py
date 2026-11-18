from collections import defaultdict
from dataclasses import asdict, dataclass, fields
from datetime import date, datetime, timedelta
from math import isfinite
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from pyracast.accounts.authorization import require_platform_admin
from pyracast.connections.models import ProviderAccount, SiteBinding
from pyracast.forecasts.jobs import binding_is_current
from pyracast.forecasts.models import (
    CollectionEndpoint,
    CollectionJob,
    CollectionJobStatus,
    ForecastSnapshot,
    HistoricalReferenceImport,
    SnapshotSourceKind,
)
from pyracast.irradiance.pairs import PowerFile, load_power
from pyracast.regions.audit import record_audit
from pyracast.regions.models import AuditAction, Region


@dataclass(frozen=True)
class HistoricalSourceSettings:
    product: str
    unit: str
    timestamp_convention: str
    period: str
    latitude: float
    longitude: float
    capacity_kw: float
    dc_capacity_kw: float
    tilt_degrees: float
    azimuth_degrees: float
    losses_percent: float
    mounting_type: str
    source_reference: str


def parse_source_settings(value: object) -> HistoricalSourceSettings:
    if not isinstance(value, dict) or set(value) != {
        field.name for field in fields(HistoricalSourceSettings)
    }:
        raise ValueError(
            "A complete historical source declaration with no unknown fields is required"
        )
    fixed = {
        "product": "historical_rooftop_pv",
        "unit": "kW",
        "timestamp_convention": "period_end",
        "period": "PT30M",
        "mounting_type": "fixed",
    }
    if any(value[key] != expected for key, expected in fixed.items()):
        raise ValueError("Unsupported historical product, units, interval convention or mounting")
    reference = value["source_reference"]
    if not isinstance(reference, str) or not reference.strip() or len(reference) > 1000:
        raise ValueError("A bounded authorization and source-settings reference is required")
    numbers: dict[str, float] = {}
    for key in (
        "latitude",
        "longitude",
        "capacity_kw",
        "dc_capacity_kw",
        "tilt_degrees",
        "azimuth_degrees",
        "losses_percent",
    ):
        number = value[key]
        if isinstance(number, bool) or not isinstance(number, int | float):
            raise ValueError("Source settings must contain finite numeric values")
        try:
            numbers[key] = float(number)
        except OverflowError as exc:
            raise ValueError("Source settings must contain finite numeric values") from exc
        if not isfinite(numbers[key]):
            raise ValueError("Source settings must contain finite numeric values")
    if (
        not -90 <= numbers["latitude"] <= 90
        or not -180 <= numbers["longitude"] <= 180
        or numbers["capacity_kw"] <= 0
        or numbers["dc_capacity_kw"] <= 0
        or numbers["tilt_degrees"] != 0
        or not -180 <= numbers["azimuth_degrees"] <= 180
        or not 0 <= numbers["losses_percent"] < 100
    ):
        raise ValueError("Invalid historical source geometry or capacity")
    return HistoricalSourceSettings(
        product=fixed["product"],
        unit=fixed["unit"],
        timestamp_convention=fixed["timestamp_convention"],
        period=fixed["period"],
        latitude=numbers["latitude"],
        longitude=numbers["longitude"],
        capacity_kw=numbers["capacity_kw"],
        dc_capacity_kw=numbers["dc_capacity_kw"],
        tilt_degrees=numbers["tilt_degrees"],
        azimuth_degrees=numbers["azimuth_degrees"],
        losses_percent=numbers["losses_percent"],
        mounting_type=fixed["mounting_type"],
        source_reference=reference.strip(),
    )


def _validate_authorized_binding(binding: SiteBinding) -> None:
    account = binding.provider_account
    if (
        not binding_is_current(binding)
        or account.provider != "solcast"
        or not account.authorization_reference
        or account.authorized_by_id is None
        or binding.validated_at is None
        or not binding.capacity_semantics_verified
        or binding.power_basis != "ac"
        or not binding.settings_verification_reference
    ):
        raise ValidationError(_("A current authorized and validated Solcast binding is required."))


def _validate_binding(binding: SiteBinding, source: HistoricalSourceSettings) -> None:
    _validate_authorized_binding(binding)
    for name in (
        "latitude",
        "longitude",
        "capacity_kw",
        "dc_capacity_kw",
        "azimuth_degrees",
        "losses_percent",
    ):
        if getattr(binding, name) != getattr(source, name):
            raise ValidationError("Declared historical source settings do not match the binding.")


def _day_indexes(power: PowerFile, zone: ZoneInfo) -> dict[date, list[int]]:
    days: dict[date, list[int]] = defaultdict(list)
    for index, interval in enumerate(power.intervals):
        day = interval.start.astimezone(zone).date()
        last = (interval.end - timedelta(microseconds=1)).astimezone(zone).date()
        while day <= last:
            days[day].append(index)
            day += timedelta(days=1)
    return days


def _prepare_region_calibrations(region_id: int) -> None:
    from pyracast.plants.models import Plant
    from pyracast.plants.preparation import prepare_calibration

    for plant_id in Plant.objects.filter(
        region_id=region_id, current_configuration__isnull=False
    ).values_list("pk", flat=True):
        prepare_calibration(plant_id)


def import_historical_reference(
    actor: User,
    tenant_id: int,
    region_id: int,
    binding_id: int,
    power_path: Path,
    source_settings: HistoricalSourceSettings,
    *,
    trusted_root: Path | None = None,
) -> HistoricalReferenceImport:
    require_platform_admin(actor)
    source = parse_source_settings(asdict(source_settings))
    power = load_power(power_path, trusted_root=trusted_root)
    if not power.intervals:
        raise ValidationError("Historical input must contain completed intervals.")
    moment = timezone.now()
    if any(interval.end > moment for interval in power.intervals):
        raise ValidationError("Historical input cannot contain future or incomplete intervals.")
    with transaction.atomic():
        region = Region.objects.select_for_update().get(
            pk=region_id, tenant_id=tenant_id, deleted_at__isnull=True
        )
        binding = SiteBinding.objects.get(pk=binding_id, region=region)
        account = ProviderAccount.objects.select_for_update().get(pk=binding.provider_account_id)
        binding = SiteBinding.objects.select_for_update().get(pk=binding.pk)
        binding.provider_account = account
        _validate_binding(binding, source)
        return _archive_historical_reference(
            actor, region, binding, power, power_path.name, asdict(source), moment
        )


def _archive_historical_reference(
    actor: User,
    region: Region,
    binding: SiteBinding,
    power: PowerFile,
    source_name: str,
    source_settings: dict[str, Any],
    moment: datetime,
) -> HistoricalReferenceImport:
    previous = HistoricalReferenceImport.objects.filter(
        binding=binding, sha256=power.sha256
    ).first()
    if previous is not None:
        if previous.source_settings != source_settings:
            raise ValidationError(_("The existing import has a different source declaration."))
        return previous
    record = HistoricalReferenceImport.objects.create(
        binding=binding,
        sha256=power.sha256,
        source_name=source_name,
        source_settings=source_settings,
        imported_by=actor,
        imported_at=moment,
        interval_count=len(power.intervals),
    )
    for day, indexes in _day_indexes(power, ZoneInfo(binding.timezone)).items():
        job = CollectionJob.objects.create(
            binding=binding,
            reference_import=record,
            endpoint=CollectionEndpoint.HISTORICAL_IMPORT,
            scheduled_slot=moment,
            target_date=day,
            status=CollectionJobStatus.SUCCEEDED,
            available_at=moment,
        )
        normalized: list[dict[str, Any]] = [
            {
                "start": power.intervals[index].start.isoformat(),
                "end": power.intervals[index].end.isoformat(),
                "power_kw": power.intervals[index].power_kw,
            }
            for index in indexes
        ]
        ForecastSnapshot.objects.create(
            job=job,
            binding=binding,
            raw_payload={
                "estimated_actuals": [power.raw_rows[index] for index in indexes],
                "historical_import": {
                    "id": record.pk,
                    "sha256": power.sha256,
                    "source_settings": source_settings,
                },
            },
            normalized_intervals=normalized,
            collected_at=moment,
            available_at=moment,
            issued_at=None,
            source_kind=SnapshotSourceKind.ESTIMATED_ACTUALS,
            coverage_start=power.intervals[indexes[0]].start,
            coverage_end=power.intervals[indexes[-1]].end,
        )
    record_audit(
        actor=actor,
        region=region,
        action=AuditAction.HISTORICAL_REFERENCE_IMPORTED,
        revision=region.revision,
    )
    transaction.on_commit(lambda: _prepare_region_calibrations(region.pk))
    return record
