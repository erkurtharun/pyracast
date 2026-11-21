from dataclasses import dataclass
from math import isfinite
from typing import TYPE_CHECKING, NoReturn, Self, TypeIs

from django.utils.translation import gettext_lazy as _

from pyracast.errors import DomainError, ErrorCode
from pyracast.plants.models import (
    CONFIGURATION_PARAMETERS,
    MeasurementBasis,
    MountingType,
    Plant,
    PlantConfiguration,
    PlantSubmission,
)
from pyracast.regions.operations import require_name

if TYPE_CHECKING:
    from django_stubs_ext import StrOrPromise

TILT_MAX_DEGREES = 90
AZIMUTH_LIMIT_DEGREES = 360
CAPACITY_REFUSED = _("Capacities must be finite and positive.")
ANGLE_REFUSED = _("Surface angles must be finite and inside the allowed bounds.")


def _refuse(field: str, message: StrOrPromise) -> NoReturn:
    raise DomainError(ErrorCode.INVALID_REQUEST, message, field=field)


def _finite(value: object) -> TypeIs[float]:
    return isinstance(value, int | float) and not isinstance(value, bool) and isfinite(value)


@dataclass(frozen=True)
class PlantParameters:
    """What a customer declares for a plant: capacities in MW, surface angles in degrees with
    the azimuth clockwise from north. An instance holds only accepted values."""

    name: str
    dc_capacity_mw: float
    inverter_capacity_mw: float | None = None
    export_limit_mw: float | None = None
    measurement_basis: str = MeasurementBasis.NET_EXPORT
    mounting_type: str = MountingType.UNKNOWN
    surface_tilt_degrees: float | None = None
    surface_azimuth_degrees: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", require_name(self.name))
        for field, optional in (
            ("dc_capacity_mw", False),
            ("inverter_capacity_mw", True),
            ("export_limit_mw", True),
        ):
            value = getattr(self, field)
            if not (optional and value is None) and not (_finite(value) and value > 0):
                _refuse(field, CAPACITY_REFUSED)
        if self.measurement_basis not in MeasurementBasis.values:
            _refuse("measurement_basis", _("Unsupported meter basis."))
        if self.mounting_type not in MountingType.values:
            _refuse("mounting_type", _("Unsupported mounting type."))
        tilt, azimuth = self.surface_tilt_degrees, self.surface_azimuth_degrees
        if tilt is not None and not (_finite(tilt) and 0 <= tilt <= TILT_MAX_DEGREES):
            _refuse("surface_tilt_degrees", ANGLE_REFUSED)
        if azimuth is not None and not (_finite(azimuth) and 0 <= azimuth < AZIMUTH_LIMIT_DEGREES):
            _refuse("surface_azimuth_degrees", ANGLE_REFUSED)
        if self.mounting_type != MountingType.FIXED and (tilt is not None or azimuth is not None):
            _refuse("mounting_type", _("Only fixed mounting permits surface angles."))

    def declared(self) -> dict[str, object]:
        return {field: getattr(self, field) for field in CONFIGURATION_PARAMETERS}

    @classmethod
    def of(cls, name: str, record: PlantSubmission | PlantConfiguration) -> Self:
        return cls(name, **{field: getattr(record, field) for field in CONFIGURATION_PARAMETERS})


def declaration(plant: Plant) -> PlantSubmission | PlantConfiguration | None:
    """The values a plant declares: its submission while that awaits approval, otherwise its
    current configuration. Reads the plant's cached relations."""
    submission: PlantSubmission | None = getattr(plant, "submission", None)
    if (
        submission is not None
        and submission.base_configuration_id == plant.current_configuration_id
    ):
        return submission
    return plant.current_configuration


def declared_parameters(plant: Plant) -> PlantParameters | None:
    record = declaration(plant)
    return None if record is None else PlantParameters.of(plant.name, record)
