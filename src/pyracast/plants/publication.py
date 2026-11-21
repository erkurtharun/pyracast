from dataclasses import dataclass
from datetime import datetime

from pyracast.connections.models import SiteBinding
from pyracast.forecasts.models import ForecastSnapshot
from pyracast.plants import forecast_archive, preparation
from pyracast.plants.forecast_archive import PreparedForecastIssuance
from pyracast.plants.models import Plant

Prepared = tuple[PreparedForecastIssuance, ...]


@dataclass(frozen=True)
class _LockedPlant:
    plant: Plant | None

    def save(self, prepared: Prepared, snapshot: ForecastSnapshot, published_at: datetime) -> None:
        forecast_archive.save_forecast_issuances(prepared, snapshot, published_at, plant=self.plant)


class PlantPublication:
    """Archives a published forecast for its region's plant; actuals ask for its calibration."""

    def prepare(self, snapshot: ForecastSnapshot) -> Prepared:
        return forecast_archive.prepare_forecast_issuances(snapshot)

    def lock(self, region_id: int) -> _LockedPlant:
        return _LockedPlant(Plant.objects.select_for_update().filter(region_id=region_id).first())

    def actuals_published(self, binding: SiteBinding, now: datetime) -> None:
        preparation.request_preparation(
            Plant.objects.filter(
                region_id=binding.region_id, current_configuration__binding=binding
            ).values_list("pk", flat=True),
            now,
        )


PUBLICATION = PlantPublication()
