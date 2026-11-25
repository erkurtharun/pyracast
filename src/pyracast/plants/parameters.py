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
CAPACITY_REFUSED = _("Enter a number greater than zero.")
TILT_REFUSED = _("Enter a panel tilt from 0 to 90°.")
AZIMUTH_REFUSED = _("Enter a panel azimuth of at least 0° and less than 360°.")
NUMBERS = (
    "dc_capacity_mw",
    "inverter_capacity_mw",
    "export_limit_mw",
    "surface_tilt_degrees",
    "surface_azimuth_degrees",
)


def _refuse(field: str, message: StrOrPromise) -> NoReturn:
    raise DomainError(ErrorCode.INVALID_REQUEST, message, field=field)


def without_negative_zero[T](value: T) -> T | float:
    """A number is its float, as PostgreSQL returns a stored double precision, and a negative
    zero is zero, whose sign PostgreSQL keeps when one is stored. A boolean is not a number."""
    if isinstance(value, int | float) and not isinstance(value, bool):
        return 0.0 if value == 0 else float(value)
    return value


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
            _refuse("measurement_basis", _("Choose a meter point."))
        if self.mounting_type not in MountingType.values:
            _refuse("mounting_type", _("Choose a mounting."))
        tilt, azimuth = self.surface_tilt_degrees, self.surface_azimuth_degrees
        if tilt is not None and not (_finite(tilt) and 0 <= tilt <= TILT_MAX_DEGREES):
            _refuse("surface_tilt_degrees", TILT_REFUSED)
        if azimuth is not None and not (_finite(azimuth) and 0 <= azimuth < AZIMUTH_LIMIT_DEGREES):
            _refuse("surface_azimuth_degrees", AZIMUTH_REFUSED)
        if self.mounting_type != MountingType.FIXED and (tilt is not None or azimuth is not None):
            _refuse("mounting_type", _("Enter panel angles only for fixed mounting."))
        for field in NUMBERS:
            object.__setattr__(self, field, without_negative_zero(getattr(self, field)))

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
