from datetime import datetime

from pyracast.plants import tracking
from pyracast.plants.models import CalibrationRun, PlantConfiguration
from pyracast.plants.numerics import MODEL_VERSION


def tracking_modeled(configuration: PlantConfiguration) -> bool:
    """Tracker geometry needs a horizontal reference whose DC capacity is known."""
    binding = configuration.binding
    return (
        configuration.mounting_type == "tracking"
        and binding.tilt_degrees == 0
        and binding.dc_capacity_kw is not None
        and binding.dc_capacity_kw > 0
        and 0 <= binding.losses_percent < 100
    )


def transfer_model(configuration: PlantConfiguration) -> str:
    return tracking.MODEL_VERSION if tracking_modeled(configuration) else MODEL_VERSION


def calibration_matches_transfer(run: CalibrationRun, configuration: PlantConfiguration) -> bool:
    recorded = run.result.get("transfer_model", MODEL_VERSION)
    return isinstance(recorded, str) and recorded == transfer_model(configuration)


def reference_power_mw(
    configuration: PlantConfiguration,
    start: datetime,
    end: datetime,
    power_kw: float,
    *,
    model_version: str | None = None,
) -> float:
    """Uncalibrated plant-basis power for one reference interval mean.

    `model_version` replays an output saved under the direct transfer.
    """
    binding = configuration.binding
    power_mw = power_kw / binding.capacity_kw * configuration.dc_capacity_mw
    if (
        model_version == MODEL_VERSION
        or not tracking_modeled(configuration)
        or binding.dc_capacity_kw is None
    ):
        return power_mw
    loss_free = power_kw / binding.dc_capacity_kw / (1 - binding.losses_percent / 100)
    return power_mw * tracking.interval_gain(
        start, end, loss_free, binding.latitude, binding.longitude
    )
