from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from pyracast.forecasts.actuals import (
    ActualCandidates,
    ArchivedActualSource,
    actual_candidates,
    apply_archived_actual_source,
)
from pyracast.irradiance import local_day_bounds
from pyracast.plants.forecasting import CalibrationTimeline, calibration_timeline, forecast_payload
from pyracast.plants.measurements import current_production_identities
from pyracast.plants.models import CalibrationRun, Plant, PlantConfiguration
from pyracast.plants.transfer import transfer_model


@dataclass(frozen=True)
class SatelliteDay:
    configuration: PlantConfiguration | None
    source: ArchivedActualSource | None
    calibration: CalibrationRun | None


@dataclass
class SatelliteData:
    """What each past day from `first` to `last` calculates its satellite data from: the
    current declaration with the calibration fitted for that period when its location archived
    the day, otherwise the declaration of that day at factor 1. Each source is read once."""

    plant: Plant
    first: date
    last: date
    cutoff: datetime
    configurations: list[PlantConfiguration]
    _candidates: dict[int, ActualCandidates] | None = field(default=None, init=False)
    _timeline: CalibrationTimeline | None = field(default=None, init=False)
    _identities: set[int] = field(default_factory=set, init=False)

    def day(self, day: date) -> SatelliteDay:
        current = next(
            (row for row in self.configurations if row.pk == self.plant.current_configuration_id),
            None,
        )
        source = self._source(current, day) if current is not None else None
        if current is not None and source is not None:
            return SatelliteDay(current, source, self._calibration(current, day))
        declared = self._declared(day)
        return SatelliteDay(
            declared, self._source(declared, day) if declared is not None else None, None
        )

    def _source(self, configuration: PlantConfiguration, day: date) -> ArchivedActualSource | None:
        if self._candidates is None:
            bindings = {row.binding_id: row.binding for row in self.configurations}
            self._candidates = actual_candidates(
                tuple(bindings.values()), self.first, self.last, self.cutoff
            )
        return self._candidates[configuration.binding_id].select(day)

    def _calibration(self, configuration: PlantConfiguration, day: date) -> CalibrationRun | None:
        if self._timeline is None:
            self._timeline = calibration_timeline(configuration, self.cutoff)
            self._identities = current_production_identities(
                [run for _, run in self._timeline.runs]
            )
        run = self._timeline.for_day(day)
        return run if run is not None and run.pk in self._identities else None

    def _declared(self, day: date) -> PlantConfiguration | None:
        """The declaration valid at the day's start, else one made during it, else the first."""
        declared = [row for row in self.configurations if row.created_at <= self.cutoff]
        start, end = local_day_bounds(day, self.plant.region.timezone)
        return (
            next((row for row in reversed(declared) if row.created_at <= start), None)
            or next((row for row in reversed(declared) if row.created_at < end), None)
            or next(iter(declared), None)
        )


def satellite_data(plant: Plant, first: date, last: date, now: datetime) -> SatelliteData:
    configurations = list(
        PlantConfiguration.objects.select_related("binding__provider_account")
        .filter(plant=plant)
        .order_by("created_at", "pk")
    )
    return SatelliteData(plant, first, last, now.astimezone(UTC), configurations)


def satellite_result(
    day: SatelliteDay, target_date: date, resolution_minutes: int
) -> dict[str, object] | None:
    configuration, source = day.configuration, day.source
    if configuration is None or source is None:
        return None
    result = forecast_payload(
        configuration,
        source.snapshot,
        day.calibration,
        target_date,
        resolution_minutes,
        model_version=transfer_model(configuration, day.calibration),
        source_intervals=source.composition.intervals if source.composition is not None else None,
    )
    apply_archived_actual_source(result, source)
    return result
