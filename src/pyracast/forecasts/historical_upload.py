import csv
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
from django.utils import timezone
from django.utils.translation import gettext as _

from pyracast.accounts.authorization import require_platform_admin
from pyracast.connections.models import SiteBinding
from pyracast.forecasts.historical_import import archive_reference, day_indexes, require_reference
from pyracast.forecasts.models import HistoricalReferenceImport
from pyracast.irradiance.solcast_export import PowerFile, PowerFileInterval, parse_json
from pyracast.regions.models import Region

MAX_UPLOAD_BYTES = 7 * 1024 * 1024
MAX_UPLOAD_ROWS = 200_000
# Solcast's historical archive begins on 1 January 2007; earlier intervals are not its data.
ARCHIVE_START = datetime(2007, 1, 1, tzinfo=UTC)
# Rooftop power never exceeds the AC reference capacity; the tolerance absorbs rounding.
CAPACITY_TOLERANCE = 1.01
# Below this share of the capacity on every interval of several days the unit is suspect.
IMPLAUSIBLY_SMALL_SHARE = 0.01
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
_FIXED_SETTINGS: dict[str, object] = {
    "tilt_degrees": 0.0,
    "mounting_type": "fixed",
    "unit": "kW",
    "timestamp_convention": "period_end",
}
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
    checks: tuple[tuple[str, bool], ...] = ()
    previous_import: HistoricalReferenceImport | None = None
    unit: str = "kW"


def _canonical(row: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in row.items():
        name = _ALIASES.get(re.sub("[^a-z0-9]", "", key.lower()), key)
        if name in result:
            raise ValidationError(_("Duplicate source columns are not supported."))
        result[name] = value
    return result


def _translated_refusal(message: str) -> str:
    match message:
        case "Nonfinite JSON values are not supported.":
            return _("Nonfinite JSON values are not supported.")
        case "Duplicate JSON object keys are not supported.":
            return _("Duplicate JSON object keys are not supported.")
        case "The JSON export is nested too deeply.":
            return _("The JSON export is nested too deeply.")
        case "The file contains a NUL character.":
            return _("The file contains a NUL character.")
    return _("The file is not valid JSON.")


def _read_rows(content: bytes, filename: str) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
    text = content.decode("utf-8-sig")
    if "\x00" in text:
        raise ValueError(_("The file contains a NUL character."))
    suffix = PureWindowsPath(filename).suffix.lower()
    if suffix == ".json":
        try:
            data = parse_json(text)
        except ValueError as error:
            raise ValueError(_translated_refusal(str(error))) from None
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
            if start < ARCHIVE_START:
                raise ValueError(_("Intervals before the provider archive starts on 2007-01-01."))
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


def _reference_setting(binding: SiteBinding, name: str) -> object:
    if name in _FIXED_SETTINGS:
        return _FIXED_SETTINGS[name]
    return binding.timezone if name == "site_timezone" else getattr(binding, name)


def preview_historical_upload(
    binding: SiteBinding, upload: HistoricalUpload
) -> HistoricalUploadPreview:
    metadata = _metadata(upload.metadata)
    verified = []
    conflicts = []
    missing = []
    for name in _SETTINGS:
        expected = _reference_setting(binding, name)
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
    days = tuple(day_indexes(upload.power, ZoneInfo(binding.timezone)))
    highest = max(interval.power_kw for interval in upload.power.intervals)
    within_capacity = highest <= binding.capacity_kw * CAPACITY_TOLERANCE
    if not within_capacity:
        conflicts.append(
            _(
                "Interval power reaches %(power)s kW, above the reference capacity of "
                "%(capacity)s kW; check that the file is in kW."
            )
            % {"power": f"{highest:g}", "capacity": f"{binding.capacity_kw:g}"}
        )
    plausible_size = len(days) < 2 or highest >= binding.capacity_kw * IMPLAUSIBLY_SMALL_SHARE
    if not plausible_size:
        warnings.append(
            "Every interval stays below 1% of the reference capacity; check the file's unit."
        )
    previous = (
        HistoricalReferenceImport.objects.filter(
            binding=binding, content_sha256=upload.power.sha256
        )
        .select_related("imported_by")
        .first()
    )
    if previous is not None:
        conflicts.append(
            _("This file was already imported on %(date)s by %(actor)s.")
            % {
                "date": previous.created_at.date().isoformat(),
                "actor": previous.imported_by.get_username(),
            }
        )
    return HistoricalUploadPreview(
        upload.power.intervals[0].start,
        upload.power.intervals[-1].end,
        len(upload.power.intervals),
        days,
        tuple(verified),
        tuple(missing),
        tuple(warnings),
        tuple(conflicts),
        upload.period,
        checks=(
            ("power_within_capacity", within_capacity),
            ("power_not_implausibly_small", plausible_size),
            ("not_imported_before", previous is None),
        ),
        previous_import=previous,
    )


def import_historical_upload(
    actor: User,
    tenant_id: int,
    region_id: int,
    binding_id: int,
    upload: HistoricalUpload,
    filename: str,
    *,
    settings_confirmed: bool,
    source_reference: str = "Administrator-confirmed historical export for the selected binding",
    expected_configuration_ids: tuple[tuple[int, int | None], ...] | None = None,
) -> tuple[HistoricalReferenceImport, bool]:
    """The one importer behind the console upload and the command; a file the binding already
    holds returns that import unchanged."""
    require_platform_admin(actor)
    if (
        settings_confirmed is not True
        or not source_reference.strip()
        or len(source_reference) > 1000
    ):
        raise ValidationError(
            _("Confirm the historical modeled product and selected reference settings.")
        )
    region = Region.objects.active().get(pk=region_id, tenant_id=tenant_id)
    binding = SiteBinding.objects.select_related("provider_account").get(
        pk=binding_id, region=region
    )
    binding.region = region
    require_reference(binding)
    preview = preview_historical_upload(binding, upload)
    if preview.previous_import is not None:
        return preview.previous_import, False
    if preview.conflicts:
        raise ValidationError(list(preview.conflicts))
    manifest = {key: _reference_setting(binding, key) for key in _SETTINGS} | {
        "product": "historical_rooftop_pv",
        "settings_matched": binding.dc_capacity_kw is not None,
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
    return archive_reference(
        actor,
        binding,
        upload.power,
        PureWindowsPath(filename).name[:255],
        manifest,
        expected_configuration_ids=expected_configuration_ids,
    )
