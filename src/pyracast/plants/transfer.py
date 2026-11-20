from datetime import datetime

from pyracast.plants import tracking
from pyracast.plants.models import CalibrationRun, PlantConfiguration
from pyracast.plants.numerics import MODEL_VERSION


def tracking_modeled(configuration: PlantConfiguration) -> bool:
    """Tracker geometry needs a horizontal reference whose DC capacity is known."""
    binding = configuration.binding
    return (
        configuration.mounting_type == "tracking"
        and binding.dc_capacity_kw is not None
        and binding.dc_capacity_kw > 0
        and 0 <= binding.losses_percent < 100
    )


def recorded_transfer(run: CalibrationRun) -> object:
    """A calibration without `transfer_model` predates the tracker transfer: direct."""
    return run.result.get("transfer_model", MODEL_VERSION)


def _tracker_model(value: object) -> str | None:
    return next((model for model in tracking.MODELS if model == value), None)


def transfer_model(
    configuration: PlantConfiguration, calibration: CalibrationRun | None = None
) -> str:
    """The model a new result is computed under: a tracker plant keeps the geometry version
    its calibration was fitted under and takes the default without one."""
    if not tracking_modeled(configuration):
        return MODEL_VERSION
    recorded = None if calibration is None else _tracker_model(recorded_transfer(calibration))
    return tracking.DEFAULT_MODEL if recorded is None else recorded


def calibration_matches_transfer(run: CalibrationRun, configuration: PlantConfiguration) -> bool:
    recorded = recorded_transfer(run)
    if tracking_modeled(configuration):
        return _tracker_model(recorded) is not None
    return recorded == MODEL_VERSION


def reference_power_mw(
    configuration: PlantConfiguration,
    start: datetime,
    end: datetime,
    power_kw: float,
    *,
    model_version: str,
) -> float:
    """Uncalibrated plant-basis power for one reference interval mean under the model a
    result records."""
    if model_version != MODEL_VERSION and model_version not in tracking.MODELS:
        raise ValueError("Unknown transfer model")
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
        start, end, loss_free, binding.latitude, binding.longitude, model=model_version
    )
