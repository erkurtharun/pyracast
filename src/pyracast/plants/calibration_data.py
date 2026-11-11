from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import cast
from zoneinfo import ZoneInfo

from django.db.models import Q

from pyracast.forecasts.history import HISTORY_SELECTION_POLICY, select_actual_snapshot
from pyracast.forecasts.models import ForecastSnapshot
from pyracast.irradiance import local_day_bounds
from pyracast.plants.availability import actual_power_intervals
from pyracast.plants.forecasting import power_limit
from pyracast.plants.incident_policy import incidents_unchanged
from pyracast.plants.measurements import (
    Measurement,
    production_identity_current,
    select_measurements,
)
from pyracast.plants.models import CalibrationRun, PlantConfiguration, ProductionObservation
from pyracast.plants.numerics import (
    DAYLIGHT_COVERAGE_POLICY,
    CalibrationSample,
    PowerInterval,
    RequiredWindows,
    evaluate_samples,
)
from pyracast.solar import DAYLIGHT_POLICY, daylight_window


@dataclass(frozen=True)
class CalibrationDataset:
    samples: tuple[CalibrationSample, ...]
    manifest: dict[str, object]
    selection: dict[str, object]
    required_windows: RequiredWindows
    incumbent_factor: float


def _daylight_windows(
    run: CalibrationRun, configuration: PlantConfiguration
) -> dict[date, tuple[tuple[datetime, datetime], ...]]:
    binding = configuration.binding
    day = run.window_start
    windows = {}
    while day <= run.window_end:
        windows[day] = daylight_window(
            day, binding.timezone, binding.latitude, binding.longitude
        ).windows
        day += timedelta(days=1)
    return windows


def _observation_manifest(
    observation: Measurement, day: str, pieces: tuple[PowerInterval, ...]
) -> dict[str, object]:
    return {
        "observation_id": observation.pk,
        "production_snapshot_id": observation.snapshot.pk if observation.snapshot else None,
        "production_digest": observation.snapshot.content_sha256 if observation.snapshot else None,
        "production_provider": observation.snapshot.provider_plant
        if observation.snapshot
        else None,
        "production_source_kind": observation.snapshot.source_kind
        if observation.snapshot
        else None,
        "production_basis": (
            "net_export"
            if observation.snapshot.source_kind == "settlement_injection"
            else "provisional_proxy"
        )
        if observation.snapshot
        else None,
        "start": observation.starts_at.isoformat(),
        "end": observation.ends_at.isoformat(),
        "energy_mwh": observation.energy_mwh,
        "quality": observation.quality,
        "source": observation.source,
        "recorded_at": observation.recorded_at.isoformat(),
        "day": day,
        "intervals": [
            {
                "start": piece.start.isoformat(),
                "end": piece.end.isoformat(),
                "power_mw": piece.power_mw,
            }
            for piece in pieces
        ],
    }


def _reference_manifest(snapshot: ForecastSnapshot) -> dict[str, object]:
    imported = snapshot.job.reference_import
    return {
        "snapshot_id": snapshot.pk,
        "binding_id": snapshot.job.binding_id,
        "source_kind": snapshot.source_kind,
        "source_product": imported.source_settings["product"]
        if imported is not None
        else "rooftop_pv_power"
        if snapshot.job.endpoint == "estimated_actuals"
        else "unknown",
        "reference_import": {
            "id": imported.pk,
            "sha256": imported.sha256,
            "source_name": imported.source_name,
            "imported_at": imported.imported_at.isoformat(),
            "source_settings": imported.source_settings,
        }
        if imported is not None
        else None,
        "collected_at": snapshot.collected_at.isoformat(),
        "available_at": cast(datetime, snapshot.available_at).isoformat(),
        "issued_at": snapshot.issued_at.isoformat() if snapshot.issued_at else None,
    }


def _reported_dates(value: object) -> list[str] | None:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None
    try:
        parsed = sorted({date.fromisoformat(item).isoformat() for item in value})
    except ValueError:
        return None
    return parsed if len(parsed) == len(value) and set(parsed) == set(value) else None


def current_source_validation(
    manifest: dict[str, object], report: dict[str, object], binding_id: int
) -> dict[str, object]:
    included = _reported_dates(report.get("included_day_dates"))
    training = _reported_dates(report.get("training_day_dates"))
    holdout = _reported_dates(report.get("holdout_day_dates"))
    selected = sorted(set(training or []) | set(holdout or []))
    sources = manifest.get("sources")
    current: list[str] = []
    if (
        isinstance(sources, dict)
        and type(manifest.get("binding_id")) is int
        and manifest["binding_id"] == binding_id
    ):
        for day in sorted(set(included or []) & set(selected)):
            source = sources.get(day)
            if (
                isinstance(source, dict)
                and type(source.get("snapshot_id")) is int
                and source["snapshot_id"] > 0
                and type(source.get("binding_id")) is int
                and source.get("binding_id") == binding_id
                and source.get("source_kind") == "estimated_actuals"
                and source.get("source_product") == "rooftop_pv_power"
                and "reference_import" in source
                and source["reference_import"] is None
            ):
                current.append(day)
    current_holdout = sorted(set(holdout or []) & set(current))
    eligible = (
        included is not None
        and training is not None
        and holdout is not None
        and type(report.get("valid_days")) is int
        and report["valid_days"] == len(included)
        and type(report.get("training_days")) is int
        and report["training_days"] == len(training)
        and report.get("holdout_days") == 7
        and len(holdout) == 7
        and holdout == included[-7:]
        and set(selected) <= set(included)
        and all(day < holdout[0] for day in training)
        and len(current) >= 30
        and len(current) == len(selected)
        and len(current_holdout) == 7
    )
    return {
        "policy": "current-rooftop-pv-power-selected-30-days-final-7-v1",
        "source_product": "rooftop_pv_power",
        "valid_day_dates": current,
        "valid_days": len(current),
        "training_day_dates": sorted(set(training or []) & set(current)),
        "training_days": len(set(training or []) & set(current)),
        "holdout_day_dates": current_holdout,
        "holdout_days": len(current_holdout),
        "eligible": eligible,
        "reference_basis": "modeled_estimated_actuals_not_measurements",
    }


def _select_reference_cohort(
    samples: tuple[CalibrationSample, ...],
    sources: dict[str, dict[str, object] | None],
    required_windows: RequiredWindows,
    configuration: PlantConfiguration,
) -> tuple[set[int], RequiredWindows, dict[str, object]]:
    products = ("rooftop_pv_power", "historical_rooftop_pv", "unknown")
    groups: dict[str, list[int]] = {product: [] for product in products}
    zone = ZoneInfo(configuration.binding.timezone)
    for index, sample in enumerate(samples):
        source = sources[sample.start.astimezone(zone).date().isoformat()]
        product = cast(str, source["source_product"]) if source is not None else "unknown"
        groups[product].append(index)
    windows_by_product: dict[str, RequiredWindows] = {}
    cohorts: dict[str, dict[str, object]] = {}
    counts: dict[str, int] = {}
    for product, indexes in groups.items():
        days = {samples[index].start.astimezone(zone).date() for index in indexes}
        windows = {day: windows for day, windows in required_windows.items() if day in days}
        windows_by_product[product] = windows
        evaluation = evaluate_samples(
            tuple(samples[index] for index in indexes),
            timezone=str(zone),
            limit_mw=power_limit(configuration),
            required_windows=windows,
        )
        counts[product] = cast(int, evaluation["valid_days"])
        cohorts[product] = {
            "complete_matched_days": counts[product],
            "complete_matched_day_dates": evaluation["included_day_dates"],
            "samples": len(indexes),
        }
    selected = next(
        (product for product in products if counts[product] >= 7),
        max(products, key=lambda product: counts[product]),
    )
    return (
        set(groups[selected]),
        windows_by_product[selected],
        {
            "policy": "single-product-native-first-seven-complete-days-v1",
            "selected_product": selected,
            "cohorts": cohorts,
            "selected_samples": len(groups[selected]),
            "excluded_samples": len(samples) - len(groups[selected]),
        },
    )


def build_calibration_dataset(
    run: CalibrationRun, configuration: PlantConfiguration
) -> CalibrationDataset:
    required_windows = _daylight_windows(run, configuration)
    incumbent_factor = _comparison_factor(run)
    zone = ZoneInfo(configuration.binding.timezone)
    start, _ = local_day_bounds(run.window_start, str(zone))
    _, end = local_day_bounds(run.window_end, str(zone))
    candidates = ProductionObservation.objects.filter(
        plant_id=run.plant_id,
        configuration=configuration,
        starts_at__gte=start,
        ends_at__lte=end,
    )
    excluded = Q(recorded_at__gt=run.cutoff_at) | Q(ends_at__gt=run.cutoff_at)
    if run.observation_id_cutoff is not None:
        excluded |= Q(pk__gt=run.observation_id_cutoff)
    late = candidates.filter(excluded).count()
    observations, _ = select_measurements(
        configuration,
        start,
        end,
        run.cutoff_at,
        observation_id_cutoff=run.observation_id_cutoff,
        production_snapshot_ids=run.production_snapshot_ids,
        policy=run.measurement_selection_policy,
        incident_revision_ids=run.incident_revision_ids,
    )
    samples = []
    entries: list[dict[str, object]] = []
    sources: dict[str, dict[str, object] | None] = {}
    native_by_day: dict[str, tuple[PowerInterval, ...]] = {}
    unmatched = 0
    for observation in observations:
        day = observation.starts_at.astimezone(zone).date()
        key = day.isoformat()
        if key not in sources:
            if run.reference_snapshot_ids is not None:
                snapshot = (
                    ForecastSnapshot.objects.select_related("job__reference_import")
                    .filter(pk=run.reference_snapshot_ids.get(key))
                    .first()
                )
            else:
                snapshot = select_actual_snapshot(configuration.binding, day, run.cutoff_at)
            sources[key] = None if snapshot is None else _reference_manifest(snapshot)
            native_by_day[key] = (
                () if snapshot is None else actual_power_intervals(snapshot, configuration)
            )
        pieces = tuple(
            PowerInterval(
                max(piece.start, observation.starts_at),
                min(piece.end, observation.ends_at),
                piece.power_mw,
            )
            for piece in native_by_day[key]
            if piece.start < observation.ends_at and piece.end > observation.starts_at
        )
        entries.append(_observation_manifest(observation, key, pieces))
        if sources[key] is None:
            unmatched += 1
        samples.append(
            CalibrationSample(
                observation.starts_at,
                observation.ends_at,
                observation.energy_mwh,
                pieces,
                observation.quality,
            )
        )
    selected, fit_windows, cohort = _select_reference_cohort(
        tuple(samples), sources, required_windows, configuration
    )
    fit_samples = tuple(sample for index, sample in enumerate(samples) if index in selected)
    fit_observations = [row for index, row in enumerate(observations) if index in selected]
    manifest: dict[str, object] = {
        "measurement_selection_policy": run.measurement_selection_policy,
        "incident_revision_ids": run.incident_revision_ids,
        "incident_policy": "exclude-whole-overlapping-meter-interval-v1",
        "metric_scope": "daylight_intervals",
        "coverage_policy": DAYLIGHT_COVERAGE_POLICY,
        "daylight_policy": DAYLIGHT_POLICY,
        "required_windows": {
            day.isoformat(): [[start.isoformat(), end.isoformat()] for start, end in windows]
            for day, windows in required_windows.items()
        },
        "comparison_activation_id": run.comparison_activation_id,
        "incumbent_factor": incumbent_factor,
        "configuration_id": configuration.pk,
        "configuration_declared_at": configuration.created_at.isoformat(),
        "configuration_time_policy": "current-declaration-applied-retrospectively-v1",
        "binding_id": configuration.binding_id,
        "dc_capacity_mw": configuration.dc_capacity_mw,
        "limit_mw": power_limit(configuration),
        "measurement_basis": configuration.measurement_basis,
        "cutoff_at": run.cutoff_at.isoformat(),
        "observation_id_cutoff": run.observation_id_cutoff,
        "window_start": run.window_start.isoformat(),
        "window_end": run.window_end.isoformat(),
        "selection_policy": run.dataset_manifest.get("selection_policy", HISTORY_SELECTION_POLICY),
        "source_kind": "estimated_actuals",
        "reference_cohort": cohort,
        "sources": sources,
        "observations": entries,
    }
    report: dict[str, object] = {
        "recorded_after_cutoff": late,
        "unmatched_observations": unmatched,
        "unmatched_days": [day for day, source in sources.items() if source is None],
        "selected_observations": len(fit_samples),
        "reference_cohort": cohort,
        "provisional_production": any(
            row.snapshot is not None and row.snapshot.source_kind == "realtime_generation"
            for row in fit_observations
        ),
        "production_sources": sorted(
            {row.source if row.snapshot else "manual" for row in fit_observations}
        ),
    }
    return CalibrationDataset(fit_samples, manifest, report, fit_windows, incumbent_factor)


def _comparison_factor(run: CalibrationRun) -> float:
    activation = run.comparison_activation
    if activation is None or activation.calibration is None:
        return 1.0
    if not incidents_unchanged(
        activation.calibration, run.cutoff_at
    ) or not production_identity_current(activation.calibration):
        return 1.0
    factor = activation.calibration.factor
    if factor is None:
        raise ValueError("The comparison calibration has no factor.")
    return factor
