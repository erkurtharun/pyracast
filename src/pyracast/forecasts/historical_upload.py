import csv
import json
import re
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from io import StringIO
from math import isfinite
from pathlib import PureWindowsPath
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from pyracast.accounts.operations import require_admin
from pyracast.connections.models import ProviderAccount, SiteBinding
from pyracast.forecasts.historical_import import (
    _archive_historical_reference,
    _day_indexes,
    _validate_authorized_binding,
)
from pyracast.forecasts.models import HistoricalReferenceImport
from pyracast.irradiance.pairs import PowerFile, PowerFileInterval
from pyracast.plants.models import Plant
from pyracast.regions.models import Region

MAX_UPLOAD_BYTES = 7 * 1024 * 1024
MAX_UPLOAD_ROWS = 200_000
_PERIODS = {"PT30M": timedelta(minutes=30), "PT60M": timedelta(hours=1)}
_SETTINGS = (
    "latitude",
    "longitude",
    "capacity_kw",
    "dc_capacity_kw",
    "tilt_degrees",
    "azimuth_degrees",
    "losses_percent",
    "mounting_type",
    "unit",
    "timestamp_convention",
    "site_timezone",
    "site_id",
    "power_basis",
)
_ALIASES = {
    re.sub("[^a-z0-9]", "", key): key
    for key in (
        *_SETTINGS,
        "period_end",
        "period_start",
        "period",
        "pv_power_rooftop",
        "pv_estimate",
        "source_timezone",
        "product",
    )
}
_ALIASES.update(
    {
        "capacity": "capacity_kw",
        "dccapacity": "dc_capacity_kw",
        "tilt": "tilt_degrees",
        "azimuth": "azimuth_degrees",
        "timezone": "timezone",
        "units": "unit",
    }
)


@dataclass(frozen=True)
class HistoricalUpload:
    power: PowerFile
    metadata: dict[str, Any]
    warnings: tuple[str, ...]
    naive_timezone: str | None
    format: str
    period: str
    source_product: str
    period_declared: bool


@dataclass(frozen=True)
class HistoricalUploadPreview:
    coverage_start: datetime
    coverage_end: datetime
    interval_count: int
    local_days: tuple[date, ...]
    verified_fields: tuple[str, ...]
    missing_fields: tuple[str, ...]
    warnings: tuple[str, ...]
    conflicts: tuple[str, ...]
    period: str
    unit: str = "kW"


def _canonical(row: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in row.items():
        name = _ALIASES.get(re.sub("[^a-z0-9]", "", key.lower()), key)
        if name in result:
            raise ValidationError(_("Duplicate source columns are not supported."))
        result[name] = value
    return result


def _read_rows(content: bytes, filename: str) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
    text = content.decode("utf-8-sig")
    suffix = PureWindowsPath(filename).suffix.lower()
    if suffix == ".json":
        data = json.loads(
            text,
            parse_constant=_invalid_constant,
            parse_float=_json_float,
            object_pairs_hook=_json_object,
        )
        _validate_json_depth(data)
        if (
            not isinstance(data, dict)
            or "forecasts" in data
            or not isinstance(data.get("estimated_actuals"), list)
        ):
            raise ValueError(
                _("Expected historical estimated_actuals, not forecasts or irradiance.")
            )
        rows = data["estimated_actuals"]
        if not all(isinstance(row, dict) for row in rows):
            raise ValueError(_("Expected interval objects."))
        return (
            rows,
            {key: value for key, value in data.items() if key != "estimated_actuals"},
            "json",
        )
    if suffix != ".csv":
        raise ValueError(_("Upload a UTF-8 JSON or CSV export."))
    reader = csv.DictReader(StringIO(text), strict=True)
    if reader.fieldnames is not None:
        _canonical(dict.fromkeys(reader.fieldnames))
        if len(set(reader.fieldnames)) != len(reader.fieldnames):
            raise ValueError(_("Duplicate source columns are not supported."))
    rows = []
    for row in reader:
        if None in row or any(value is None for value in row.values()):
            raise ValueError(_("CSV rows must match the column header."))
        rows.append(dict(row))
    return rows, {}, "csv"


def _invalid_constant(value: str) -> object:
    raise ValueError(_("Nonfinite JSON values are not supported."))


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError(_("Duplicate JSON object keys are not supported."))
    return result


def _json_float(value: str) -> float:
    number = float(value)
    if not isfinite(number):
        raise ValueError(_("Nonfinite JSON values are not supported."))
    return number


def _validate_json_depth(value: object) -> None:
    pending = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if depth > 32:
            raise ValueError(_("The JSON export is nested too deeply."))
        if isinstance(item, dict):
            pending.extend((nested, depth + 1) for nested in item.values())
        elif isinstance(item, list):
            pending.extend((nested, depth + 1) for nested in item)


def _instant(value: object, zone: ZoneInfo | None) -> tuple[datetime, bool]:
    if not isinstance(value, str) or not re.search(r"\d[T ]\d", value):
        raise ValueError(_("Invalid period timestamp."))
    instant = datetime.fromisoformat(value)
    naive = instant.utcoffset() is None
    if naive:
        if zone is None:
            raise ValueError(_("Naive timestamps require an explicit source timezone."))
        candidates = {
            aware.astimezone(UTC)
            for fold in (0, 1)
            if (aware := instant.replace(tzinfo=zone, fold=fold))
            .astimezone(UTC)
            .astimezone(zone)
            .replace(tzinfo=None)
            == instant
        }
        if len(candidates) != 1:
            raise ValueError(_("Ambiguous or nonexistent local source timestamp."))
        return candidates.pop(), True
    return instant.astimezone(UTC), False


def parse_historical_upload(
    content: bytes,
    filename: str,
    *,
    source_timezone: str | None = None,
    period: str | None = None,
) -> HistoricalUpload:
    try:
        if not content or len(content) > MAX_UPLOAD_BYTES:
            raise ValueError(_("Historical upload must contain between 1 byte and 7 MiB."))
        zone = ZoneInfo(source_timezone) if source_timezone else None
        rows, metadata, file_format = _read_rows(content, filename)
        if rows:
            for key, value in _canonical(rows[0]).items():
                if key in {"timezone", "source_timezone"} and key not in metadata:
                    metadata[key] = value
        metadata_values = _metadata(metadata)
        file_timezone = metadata_values.get("source_timezone", metadata_values.get("timezone"))
        if zone is None and isinstance(file_timezone, str):
            with suppress(ValueError, ZoneInfoNotFoundError):
                zone = ZoneInfo(file_timezone)
        if not rows or len(rows) > MAX_UPLOAD_ROWS:
            raise ValueError(_("Historical upload must contain between 1 and 200000 intervals."))
        intervals: list[PowerFileInterval] = []
        products: set[str] = set()
        periods: set[str] = set()
        used_naive = False
        declared_period = False
        for raw in rows:
            row = _canonical(raw)
            keys = [key for key in ("pv_power_rooftop", "pv_estimate") if key in row]
            if len(keys) != 1:
                raise ValueError(_("Expected exactly one Solcast rooftop power column in kW."))
            power_value = row[keys[0]]
            if isinstance(power_value, bool) or not isinstance(power_value, str | int | float):
                raise ValueError(_("Power must be a finite nonnegative number in kW."))
            power = float(power_value)
            if not isfinite(power) or power < 0:
                raise ValueError(_("Power must be a finite nonnegative number in kW."))
            end, naive = _instant(row.get("period_end"), zone)
            used_naive |= naive
            file_period = row.get("period") or metadata_values.get("period")
            row_period = file_period or period
            if row_period is None and row.get("period_start"):
                explicit_start, start_naive = _instant(row["period_start"], zone)
                used_naive |= start_naive
                row_period = next(
                    (key for key, duration in _PERIODS.items() if end - explicit_start == duration),
                    None,
                )
            if row_period not in _PERIODS:
                raise ValueError(_("An explicit PT30M or PT60M interval period is required."))
            declared_period |= not bool(file_period) and period is not None
            duration = _PERIODS[row_period]
            start = end - duration
            if row.get("period_start"):
                explicit_start, start_naive = _instant(row["period_start"], zone)
                used_naive |= start_naive
                if explicit_start != start:
                    raise ValueError(
                        _("Period start/end bounds disagree with the interval duration.")
                    )
            if intervals and start < intervals[-1].end:
                raise ValueError(_("Historical intervals must be sorted and non-overlapping."))
            if end > timezone.now():
                raise ValueError(
                    _("Historical input cannot contain future or incomplete intervals.")
                )
            intervals.append(PowerFileInterval(start, end, power))
            products.add(keys[0])
            periods.add(row_period)
            for name in (*_SETTINGS, "timezone", "source_timezone", "product"):
                if name in row and row[name] != "":
                    if name in metadata and metadata[name] != row[name]:
                        raise ValueError(_("Source settings change within the uploaded file."))
                    metadata[name] = row[name]
        if used_naive and source_timezone and file_timezone and source_timezone != file_timezone:
            raise ValueError(_("Selected timezone conflicts with the file's source timezone."))
        if len(periods) != 1 or len(products) != 1:
            raise ValueError(_("Use one power product and one interval period per upload."))
        warnings = []
        if declared_period:
            warnings.append(
                "Interval duration was declared by the operator, not provided by every row."
            )
        if used_naive:
            warnings.append("Naive source timestamps were interpreted using the selected timezone.")
        return HistoricalUpload(
            PowerFile(tuple(intervals), tuple(rows), sha256(content).hexdigest()),
            metadata,
            tuple(warnings),
            zone.key if used_naive and zone else None,
            file_format,
            periods.pop(),
            "historical_rooftop_pv" if products.pop() == "pv_power_rooftop" else "rooftop_pv_power",
            declared_period,
        )
    except RecursionError as exc:
        raise ValidationError(_("The JSON export is nested too deeply.")) from exc
    except (
        ValueError,
        TypeError,
        OverflowError,
        UnicodeError,
        csv.Error,
        ZoneInfoNotFoundError,
    ) as exc:
        raise ValidationError(str(exc)) from exc


def _metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    result = _canonical(metadata)
    for container in ("metadata", "source_settings"):
        nested = result.get(container, {})
        if not isinstance(nested, dict):
            raise ValidationError(_("Source metadata must be an object."))
        for key, value in _canonical(nested).items():
            if key in result and result[key] != value:
                raise ValidationError(_("Conflicting source metadata declarations."))
            result[key] = value
    return result


def preview_historical_upload(
    binding: SiteBinding, upload: HistoricalUpload
) -> HistoricalUploadPreview:
    metadata = _metadata(upload.metadata)
    verified = []
    conflicts = []
    missing = []
    for name in _SETTINGS:
        expected = (
            "kW"
            if name == "unit"
            else "period_end"
            if name == "timestamp_convention"
            else "fixed"
            if name == "mounting_type"
            else binding.timezone
            if name == "site_timezone"
            else getattr(binding, name)
        )
        value = metadata.get(name)
        if value is None or value == "":
            missing.append(name)
            continue
        if name == "dc_capacity_kw" and expected is None:
            declared_dc = float("nan")
            with suppress(ValueError, TypeError, OverflowError):
                declared_dc = float(value)
            if not isinstance(value, bool) and isfinite(declared_dc) and declared_dc > 0:
                missing.append(name)
                continue
        if isinstance(expected, float | int) and not isinstance(value, bool):
            with suppress(ValueError, TypeError, OverflowError):
                value = float(value)
        if isinstance(value, bool) or expected is None or value != expected:
            conflicts.append(
                _("Source metadata conflicts with the selected reference: %(field)s.")
                % {"field": name}
            )
        else:
            verified.append(name)
    for name, expected in (("period", upload.period), ("product", upload.source_product)):
        if name in metadata:
            if metadata[name] == expected:
                verified.append(name)
            else:
                conflicts.append(
                    _("Source metadata conflicts with the uploaded intervals: %(field)s.")
                    % {"field": name}
                )
    warnings = list(upload.warnings)
    if missing:
        warnings.append(
            "The file does not verify all reference settings; operator confirmation is required."
        )
    if binding.dc_capacity_kw is None:
        warnings.append(
            "The selected reference has no declared DC capacity; settings matching is incomplete."
        )
    return HistoricalUploadPreview(
        upload.power.intervals[0].start,
        upload.power.intervals[-1].end,
        len(upload.power.intervals),
        tuple(_day_indexes(upload.power, ZoneInfo(binding.timezone))),
        tuple(verified),
        tuple(missing),
        tuple(warnings),
        tuple(conflicts),
        upload.period,
    )


def import_historical_upload(
    actor: User,
    tenant_id: int,
    region_id: int,
    binding_id: int,
    content: bytes,
    filename: str,
    *,
    source_timezone: str | None = None,
    period: str | None = None,
    settings_confirmed: bool,
    source_reference: str = "Administrator-confirmed historical export for the selected binding",
    expected_configuration_ids: tuple[tuple[int, int | None], ...] | None = None,
) -> HistoricalReferenceImport:
    require_admin(actor)
    if (
        settings_confirmed is not True
        or not source_reference.strip()
        or len(source_reference) > 1000
    ):
        raise ValidationError(
            _("Confirm the historical modeled product and selected reference settings.")
        )
    upload = parse_historical_upload(
        content, filename, source_timezone=source_timezone, period=period
    )
    with transaction.atomic():
        region = Region.objects.select_for_update().get(
            pk=region_id, tenant_id=tenant_id, deleted_at__isnull=True
        )
        if expected_configuration_ids is not None:
            configurations = tuple(
                Plant.objects.select_for_update()
                .filter(region=region, deleted_at__isnull=True)
                .order_by("pk")
                .values_list("pk", "current_configuration_id")
            )
            if configurations != expected_configuration_ids:
                raise ValidationError(_("The plant configuration changed. Preview the file again."))
        binding = SiteBinding.objects.get(pk=binding_id, region=region)
        account = ProviderAccount.objects.select_for_update().get(pk=binding.provider_account_id)
        binding = SiteBinding.objects.select_for_update().get(pk=binding.pk)
        binding.provider_account = account
        _validate_authorized_binding(binding)
        preview = preview_historical_upload(binding, upload)
        if preview.conflicts:
            raise ValidationError(list(preview.conflicts))
        manifest = {
            key: binding.timezone if key == "site_timezone" else getattr(binding, key)
            for key in _SETTINGS
            if key not in {"unit", "timestamp_convention", "mounting_type"}
        } | {
            "product": "historical_rooftop_pv",
            "settings_matched": binding.dc_capacity_kw is not None,
            "unit": "kW",
            "timestamp_convention": "period_end",
            "mounting_type": "fixed",
            "period": upload.period,
            "source_reference": source_reference.strip(),
            "upload_provenance": {
                "transport": upload.format,
                "file_product": upload.source_product,
                "file_metadata": upload.metadata,
                "file_verified_fields": list(preview.verified_fields),
                "operator_declared_fields": list(preview.missing_fields),
                "operator_confirmed": True,
                "binding_id": binding.pk,
                "binding_revision": binding.region_revision,
                "binding_dc_capacity_known": binding.dc_capacity_kw is not None,
                "source_timezone": upload.naive_timezone,
                "period_declared": upload.period_declared,
                "warnings": list(preview.warnings),
            },
        }
        return _archive_historical_reference(
            actor,
            region,
            binding,
            upload.power,
            PureWindowsPath(filename).name[:255],
            manifest,
            timezone.now(),
        )
