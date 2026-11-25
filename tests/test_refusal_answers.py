from datetime import timedelta
from typing import Any

import pytest
from allauth.mfa.recovery_codes.internal.auth import RecoveryCodes
from django.core.exceptions import ValidationError
from django.test import Client
from django.utils import timezone, translation

from pyracast.accounts.invitations import accept_workspace_invitation, invite_workspace_member
from pyracast.accounts.models import WorkspaceInvitation
from pyracast.connections.models import ProviderAccount
from pyracast.errors import DomainError
from pyracast.forecasts.schedule import configure_schedule
from pyracast.incidents.operations import save_incident
from pyracast.plants.operations import create_plant, revise_plant
from pyracast.plants.parameters import PlantParameters
from pyracast.plants.production_changes import validate_batch
from pyracast.regions.operations import request_location_change
from tests.support.admin_mfa_bootstrap import TEST_PASSWORD
from tests.support.admin_recovery_security import AUTH, RECOVERY, authenticated_account, submit
from tests.support.domain import NOW, System, make_system
from tests.support.plant_operations import make_plant, ready_run
from tests.support.workspaces import (
    administered_workspace,
    invitation_token,
    signed_in,
    verified_account,
)

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])

LANGUAGES = ("en", "tr")
CHECK = {
    "en": "Check the highlighted values and try again.",
    "tr": "\u0130şaretli değerleri kontrol edip tekrar deneyin.",
}
NAME = {
    "en": "The plant name contains an invisible character. Type the name again.",
    "tr": "Santral ad\u0131nda görünmeyen bir karakter var. Ad\u0131 yeniden yaz\u0131n.",
}
TILT = {
    "en": "Enter a panel tilt from 0 to 90°.",
    "tr": "0 ile 90° aras\u0131nda bir panel eğimi girin.",
}
AZIMUTH = {
    "en": "Enter a panel azimuth of at least 0° and less than 360°.",
    "tr": "0° veya daha büyük, 360°'den küçük bir panel azimutu girin.",
}
ROW_TIME = {
    "en": "Row 3: the date or time is not valid.",
    "tr": "Sat\u0131r 3: tarih veya saat geçerli değil.",
}
VALUE = {"en": "Enter a value.", "tr": "Bir değer girin."}


def language(value: str) -> dict[str, str]:
    return {"Accept-Language": value}


def member_client(system: System) -> Client:
    client = Client()
    client.force_login(system.member)
    return client


def plant_url(system: System, plant_id: int) -> str:
    return f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/plants/{plant_id}/"


def refusal_text(call: Any, accept: str) -> list[str]:
    with translation.override(accept), pytest.raises(ValidationError) as raised:
        call()
    return raised.value.messages


@pytest.mark.parametrize("accept", LANGUAGES)
@pytest.mark.parametrize("form", ["site", "plant"])
@pytest.mark.parametrize(
    ("change", "field", "texts"),
    [
        ({"name": "Solar\x00 One"}, "name", NAME),
        ({"surface_tilt_degrees": 91}, "surface_tilt_degrees", TILT),
        ({"surface_tilt_degrees": -1}, "surface_tilt_degrees", TILT),
        ({"surface_azimuth_degrees": 360}, "surface_azimuth_degrees", AZIMUTH),
        ({"surface_azimuth_degrees": -1}, "surface_azimuth_degrees", AZIMUTH),
    ],
)
def test_the_plant_form_answers_a_refused_name_or_angle_with_its_own_sentence(
    accept: str, form: str, change: dict[str, object], field: str, texts: dict[str, str]
) -> None:
    system = make_system(approved=True)
    values: dict[str, object] = {
        "name": "Plant",
        "dc_capacity_mw": 10,
        "mounting_type": "fixed",
        "surface_tilt_degrees": 30,
        "surface_azimuth_degrees": 180,
    }
    path = f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/plants/"
    if form == "site":
        values |= {"latitude": 41, "longitude": 30, "timezone": "Europe/Istanbul"}
        path = f"/api/tenants/{system.tenant.pk}/sites/"

    response = member_client(system).post(
        path, values | change, content_type="application/json", headers=language(accept)
    )

    assert (response.status_code, response.json()) == (
        400,
        {"code": "invalid_request", "message": CHECK[accept], "fields": {field: texts[accept]}},
    )


def production_row(**changes: object) -> dict[str, object]:
    return {
        "starts_at": "2026-01-05T09:00:00+03:00",
        "ends_at": "2026-01-05T10:00:00+03:00",
        "energy": 1,
        "quality": "valid",
        "source": "operator",
    } | changes


@pytest.mark.parametrize("accept", LANGUAGES)
def test_a_production_file_row_with_a_date_that_does_not_exist_names_its_row(
    monkeypatch: pytest.MonkeyPatch, accept: str
) -> None:
    system, plant = make_plant(monkeypatch)
    impossible = production_row(
        starts_at="2026-02-30T09:00:00+03:00", ends_at="2026-02-30T10:00:00+03:00"
    )

    response = member_client(system).post(
        f"{plant_url(system, plant.pk)}production/preview/",
        {
            "configuration_id": plant.current_configuration_id,
            "unit": "MWh",
            "observations": [production_row(), impossible],
        },
        content_type="application/json",
        headers=language(accept),
    )

    assert (response.status_code, response.json()) == (
        400,
        {
            "code": "invalid_request",
            "message": ROW_TIME[accept],
            "fields": {
                "observations.1.starts_at": ROW_TIME[accept],
                "observations.1.ends_at": ROW_TIME[accept],
            },
        },
    )


@pytest.mark.parametrize("accept", LANGUAGES)
@pytest.mark.parametrize("place", ["file", "changes"])
def test_a_missing_production_value_asks_for_a_value_in_every_place(
    monkeypatch: pytest.MonkeyPatch, accept: str, place: str
) -> None:
    system, plant = make_plant(monkeypatch)
    row = production_row()
    del row["energy"]
    if place == "file":
        path, rows, key = "preview/", {"observations": [row]}, "observations"
    else:
        path, key = "changes/", "changes"
        rows = {"changes": [row | {"expected_revision_id": None, "action": "set"}]}

    response = member_client(system).post(
        f"{plant_url(system, plant.pk)}production/{path}",
        {"configuration_id": plant.current_configuration_id, "unit": "MWh", **rows},
        content_type="application/json",
        headers=language(accept),
    )

    assert (response.status_code, response.json()) == (
        400,
        {
            "code": "invalid_request",
            "message": CHECK[accept],
            "fields": {f"{key}.0.energy": VALUE[accept]},
        },
    )


@pytest.mark.parametrize(
    ("accept", "text"),
    [
        (
            "en",
            "This calibration was calculated for earlier technical details. "
            "Calculate a new calibration.",
        ),
        (
            "tr",
            "Bu kalibrasyon önceki teknik bilgilerle hesapland\u0131. "
            "Yeni kalibrasyon hesaplay\u0131n.",
        ),
    ],
)
def test_a_calibration_of_earlier_technical_details_asks_for_a_new_calibration(
    monkeypatch: pytest.MonkeyPatch, accept: str, text: str
) -> None:
    system, plant = make_plant(monkeypatch)
    run = ready_run(system, plant)
    revise_plant(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        PlantParameters(name="Changed", dc_capacity_mw=140),
    )

    response = member_client(system).post(
        f"{plant_url(system, plant.pk)}calibrations/{run.pk}/activate/",
        {},
        content_type="application/json",
        headers=language(accept),
    )

    assert (response.status_code, response.json()) == (
        400,
        {"code": "configuration_changed", "message": text},
    )


@pytest.mark.parametrize(
    ("accept", "text"),
    [
        ("en", "No new forecast data is arriving for this plant."),
        ("tr", "Bu santral için yeni tahmin verisi gelmiyor."),
    ],
)
def test_a_calibration_of_a_plant_whose_connection_is_blocked_says_no_data_arrives(
    monkeypatch: pytest.MonkeyPatch, accept: str, text: str
) -> None:
    system, plant = make_plant(monkeypatch)
    run = ready_run(system, plant)
    ProviderAccount.objects.filter(pk=system.account.pk).update(enabled=False)

    response = member_client(system).post(
        f"{plant_url(system, plant.pk)}calibrations/{run.pk}/activate/",
        {},
        content_type="application/json",
        headers=language(accept),
    )

    assert (response.status_code, response.json()) == (
        400,
        {"code": "configuration_changed", "message": text},
    )


@pytest.mark.parametrize(
    ("accept", "text"),
    [
        ("en", "This invitation was already accepted. Reload the list."),
        ("tr", "Bu davet zaten kabul edildi. Listeyi yenileyin."),
    ],
)
@pytest.mark.parametrize("action", ["resend", "cancel"])
def test_the_administrator_is_told_an_invitation_was_already_accepted(
    accept: str, text: str, action: str
) -> None:
    tenant, administrator = administered_workspace()
    invite_workspace_member(administrator, tenant.pk, "joined@example.com")
    accept_workspace_invitation(
        verified_account("joined", "joined@example.com"), invitation_token()
    )
    invitation = WorkspaceInvitation.objects.get(email="joined@example.com")
    path = f"/api/tenants/{tenant.pk}/invitations/{invitation.pk}/"
    client = signed_in(administrator)

    if action == "resend":
        response = client.post(f"{path}resend/", headers=language(accept))
    else:
        response = client.delete(path, headers=language(accept))

    assert (response.status_code, response.json()) == (
        400,
        {"code": "invitation_unavailable", "message": text},
    )


@pytest.mark.parametrize(
    ("accept", "text"),
    [
        ("en", "Enter your authenticator code or a recovery code."),
        ("tr", "Uygulama kodunuzu veya bir kurtarma kodunu girin."),
    ],
)
def test_the_second_step_names_both_codes_that_confirm_it(accept: str, text: str) -> None:
    client, user, _ = authenticated_account(privileged=False)
    RecoveryCodes.activate(user)
    assert (
        submit(client, AUTH + "auth/reauthenticate", {"password": TEST_PASSWORD}).status_code == 403
    )

    response = client.get(RECOVERY, headers=language(accept))

    assert response.status_code == 401
    assert response.json()["errors"] == [{"code": "mfa_reauthentication_required", "message": text}]


@pytest.mark.parametrize(
    ("unit", "count", "accept", "text"),
    [
        ("W", 1, "en", "Something went wrong. Try again."),
        ("MWh", 0, "en", "Save 1 to 2,000 values at a time."),
        ("MWh", 2001, "tr", "Tek seferde 1 ile 2.000 aras\u0131nda değer kaydedin."),
    ],
)
def test_a_wrong_unit_and_a_wrong_count_get_different_sentences(
    unit: str, count: int, accept: str, text: str
) -> None:
    assert refusal_text(lambda: validate_batch(unit, count), accept) == [text]


@pytest.mark.parametrize(
    ("times", "accept", "text"),
    [
        (["07:00", "07:00"], "en", "Use each update time once."),
        (["07:00", "07:00"], "tr", "Her güncelleme saatini bir kez kullan\u0131n."),
        (["06:00", "07:00", "08:00", "09:00"], "en", "Choose at most 3 update times."),
        (["06:00", "07:00", "08:00", "09:00"], "tr", "En fazla 3 güncelleme saati seçin."),
    ],
)
def test_duplicate_and_too_many_update_times_get_different_sentences(
    times: list[str], accept: str, text: str
) -> None:
    system = make_system(approved=True)
    ProviderAccount.objects.filter(pk=system.account.pk).update(daily_quota=4)

    def configure() -> object:
        return configure_schedule(
            system.member, system.tenant.pk, system.region.pk, enabled=True, times=times, now=NOW
        )

    assert refusal_text(configure, accept) == [text]


@pytest.mark.parametrize(
    ("accept", "reason", "notes"),
    [
        (
            "en",
            "Enter a reason of at most 1,000 characters.",
            "Enter notes of at most 2,000 characters.",
        ),
        (
            "tr",
            "En fazla 1.000 karakterlik bir gerekçe girin.",
            "En fazla 2.000 karakterlik bir not girin.",
        ),
    ],
)
def test_character_limits_are_written_with_the_language_grouping(
    accept: str, reason: str, notes: str
) -> None:
    system = make_system(approved=True)
    plant = create_plant(
        system.member, system.tenant.pk, system.region.pk, PlantParameters("PV", 1)
    )

    def long_reason() -> object:
        return request_location_change(
            system.member,
            system.region.pk,
            system.tenant.pk,
            expected_revision=system.region.revision,
            latitude=41,
            longitude=30,
            timezone="UTC",
            reason="x" * 1001,
        )

    def long_notes() -> object:
        return save_incident(
            system.member,
            system.tenant.pk,
            system.region.pk,
            plant.pk,
            kind="other",
            title="Loss",
            notes="x" * 2001,
            starts_at=timezone.now() - timedelta(hours=2),
            ends_at=None,
            available_dc_mw=None,
            available_inverter_mw=None,
            available_export_mw=None,
        )

    assert refusal_text(long_reason, accept) == [reason]
    with translation.override(accept), pytest.raises(DomainError) as refused:
        long_notes()
    with translation.override(accept):
        assert (refused.value.field, refused.value.messages) == ("notes", [notes])


@pytest.mark.parametrize("accept", LANGUAGES)
@pytest.mark.parametrize(("form", "field"), [("plant", "dc_capacity_mw"), ("site", "latitude")])
def test_a_missing_number_in_the_plant_form_asks_for_a_value(
    accept: str, form: str, field: str
) -> None:
    system = make_system(approved=True)
    values: dict[str, object] = {"name": "Plant", "dc_capacity_mw": 10}
    path = f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/plants/"
    if form == "site":
        values |= {"latitude": 41, "longitude": 30, "timezone": "Europe/Istanbul"}
        path = f"/api/tenants/{system.tenant.pk}/sites/"
    del values[field]

    response = member_client(system).post(
        path, values, content_type="application/json", headers=language(accept)
    )

    assert (response.status_code, response.json()) == (
        400,
        {"code": "invalid_request", "message": CHECK[accept], "fields": {field: VALUE[accept]}},
    )
