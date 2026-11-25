from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.core.exceptions import ValidationError
from django.utils import translation
from rest_framework import serializers

from pyracast.api.serializers import InputSerializer
from pyracast.errors import DomainError
from pyracast.plants.forecasting import require_known_cutoff
from pyracast.plants.models import MountingType
from pyracast.plants.operations import EDIT_BLOCK_MESSAGES, PlantEditBlock
from pyracast.plants.parameters import PlantParameters
from pyracast.plants.production_changes import ObservationInput, normalize_observation
from pyracast.regions.operations import require_name, validate_location

NOW = datetime(2026, 6, 1, 12, tzinfo=UTC)
GENERIC = {"en": "Something went wrong. Try again.", "tr": "Bir sorun oluştu. Tekrar deneyin."}


class NamedInput(InputSerializer):
    name = serializers.CharField()


def refusal(call: Callable[[], object], language: str) -> dict[str, list[str]]:
    with translation.override(language):
        with pytest.raises(ValidationError) as raised:
            call()
        error = raised.value
        if isinstance(error, DomainError) and error.field:
            return {error.field: error.messages}
        return {"": error.messages}


def unzoned_production() -> None:
    start = datetime(2026, 5, 1, 10)
    observation = ObservationInput(start, start + timedelta(hours=1), 1.0, "valid", "operator")
    normalize_observation(observation, "MWh", ZoneInfo("Europe/Istanbul"), NOW)


def unzoned_cutoff() -> None:
    require_known_cutoff(datetime(2026, 5, 1, 10), NOW)


@pytest.mark.parametrize("language", ["en", "tr"])
@pytest.mark.parametrize("guard", [unzoned_production, unzoned_cutoff])
def test_input_only_a_broken_client_sends_answers_the_generic_sentence(
    guard: Callable[[], None], language: str
) -> None:
    assert refusal(guard, language) == {"": [GENERIC[language]]}


@pytest.mark.parametrize("language", ["en", "tr"])
def test_an_unknown_field_answers_the_generic_sentence(language: str) -> None:
    serializer = NamedInput(data={"name": "Plant", "colour": "red"})
    with translation.override(language):
        assert not serializer.is_valid()
        assert serializer.errors == {"non_field_errors": [GENERIC[language]]}


@pytest.mark.parametrize(
    ("language", "text"),
    [
        ("en", "The plant name contains an invisible character. Type the name again."),
        ("tr", "Santral ad\u0131nda görünmeyen bir karakter var. Ad\u0131 yeniden yaz\u0131n."),
    ],
)
def test_a_plant_name_with_an_invisible_character_asks_to_type_it_again(
    language: str, text: str
) -> None:
    assert refusal(lambda: require_name("Solar\x00 One"), language) == {"name": [text]}


@pytest.mark.parametrize(
    ("language", "text"),
    [("en", "Choose a time zone."), ("tr", "Bir saat dilimi seçin.")],
)
def test_an_unknown_time_zone_asks_to_choose_one(language: str, text: str) -> None:
    assert refusal(lambda: validate_location(40, 29, "Mars/Base"), language) == {"timezone": [text]}


@pytest.mark.parametrize(
    ("language", "tilt", "azimuth"),
    [
        (
            "en",
            "Enter a panel tilt from 0 to 90°.",
            "Enter a panel azimuth of at least 0° and less than 360°.",
        ),
        (
            "tr",
            "0 ile 90° aras\u0131nda bir panel eğimi girin.",
            "0° veya daha büyük, 360°'den küçük bir panel azimutu girin.",
        ),
    ],
)
def test_each_panel_angle_refusal_names_the_range_of_its_own_angle(
    language: str, tilt: str, azimuth: str
) -> None:
    def declared(tilt_degrees: float | None, azimuth_degrees: float | None) -> Callable[[], object]:
        return lambda: PlantParameters(
            name="Plant",
            dc_capacity_mw=10,
            mounting_type=MountingType.FIXED,
            surface_tilt_degrees=tilt_degrees,
            surface_azimuth_degrees=azimuth_degrees,
        )

    assert refusal(declared(91, None), language) == {"surface_tilt_degrees": [tilt]}
    assert refusal(declared(None, 360), language) == {"surface_azimuth_degrees": [azimuth]}


@pytest.mark.parametrize(
    ("language", "suspended", "connection"),
    [
        (
            "en",
            "Pyracast suspended this plant, so its technical details cannot be changed.",
            "No new forecast data is arriving for this plant, "
            "so its technical details cannot be changed yet.",
        ),
        (
            "tr",
            "Pyracast bu santrali ask\u0131ya ald\u0131; bu nedenle teknik bilgileri "
            "değiştirilemez.",
            "Bu santral için yeni tahmin verisi gelmiyor; "
            "bu nedenle teknik bilgileri henüz değiştirilemez.",
        ),
    ],
)
def test_a_blocked_technical_details_edit_says_why_it_is_blocked(
    language: str, suspended: str, connection: str
) -> None:
    with translation.override(language):
        assert str(EDIT_BLOCK_MESSAGES[PlantEditBlock.SUSPENDED]) == suspended
        assert str(EDIT_BLOCK_MESSAGES[PlantEditBlock.CONNECTION_UNAVAILABLE]) == connection
