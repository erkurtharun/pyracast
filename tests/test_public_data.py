from copy import deepcopy
from io import BytesIO
from typing import Any
from zipfile import ZipFile

import pytest
from django.test import Client
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory

from pyracast.api.public import public_source_data
from pyracast.api.renderers import CustomerJSONRenderer
from pyracast.connections.models import ProviderAccount
from pyracast.connections.secrets import encrypt_secret
from pyracast.notifications.mail import build_message
from pyracast.plants.models import CalibrationRun
from tests.support.domain import NOW
from tests.support.notifications import attached_sheet, ready
from tests.support.plant_operations import make_plant


def test_public_projection_preserves_customer_text_and_production_attribution() -> None:
    original: dict[str, Any] = {
        "name": "Solcast user's region",
        "email": "solcast@example.com",
        "username": "solcast-user",
        "recipient": "solcast@example.com",
        "observations": [{"starts_at": "2026-11-09T00:00:00Z", "source": "My Solcast note"}],
        "provider": "epias",
        "provider_plant_id": 42,
        "source_kind": "realtime_generation",
        "result": {
            "quality_flags": ("provisional_production_calibration", "unvalidated"),
            "energy_mwh": None,
            "complete": False,
        },
    }
    public = public_source_data(original)
    assert public == {
        **original,
        "result": {
            **original["result"],
            "quality_flags": ["provisional_production_calibration", "unvalidated"],
        },
    }
    assert original["result"]["quality_flags"] == (
        "provisional_production_calibration",
        "unvalidated",
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("solcast_forecast", "forecast"),
        ("solcast_estimated_actuals", "estimated_actuals"),
        ("unknown Solcast metadata", None),
    ],
)
def test_legacy_source_kinds_have_public_aliases(value: str, expected: str | None) -> None:
    assert public_source_data({"source_kind": value}) == {"source_kind": expected}


@pytest.mark.parametrize(
    ("flag", "expected"),
    [
        ("solcast_ambiguous_capacity_basis", "reference_ambiguous_capacity_basis"),
        ("SOLCAST_clipped", "reference_clipped"),
    ],
)
def test_private_quality_flags_preserve_all_qualifiers(flag: str, expected: str) -> None:
    assert public_source_data({"quality_flags": [flag]}) == {"quality_flags": [expected]}


def test_legacy_quality_warnings_keep_severity_and_unknown_flags_remain_visible() -> None:
    assert public_source_data(
        {
            "quality_flags": [
                "solcast_unknown",
                "solcast_provisional_unvalidated_incomplete_missing_stale_not_metered",
            ]
        }
    ) == {
        "quality_flags": [
            "reference_unknown",
            "reference_provisional_unvalidated_incomplete_missing_stale_not_metered",
        ]
    }
    assert public_source_data(
        {"detail": {"Solcast error": "private", "message": "Solcast failed"}}
    ) == {"detail": {"message": None}}
    assert public_source_data({"result": {"solcast_ghi_wm2": 50, "solcast_kwh_m2": None}}) == {
        "result": {"reference_ghi_wm2": 50, "reference_kwh_m2": None}
    }
    assert public_source_data({"result": {"provider": {"name": "Solcast"}, "name": "Solcast"}}) == {
        "result": {"name": None}
    }
    assert public_source_data({"provider": None}) == {}


def test_renderer_sanitizes_every_response() -> None:
    renderer = CustomerJSONRenderer()
    factory = APIRequestFactory()
    data = {"detail": "Solcast authentication failed", "provider_account_id": 42}
    assert renderer.render(data) == b'{"detail":null}'
    for path in ("/api/tenants/1/", "/api/platform/accounts/"):
        assert renderer.render(
            data, renderer_context={"request": Request(factory.get(path))}
        ) == renderer.render(data)
    assert (
        renderer.render({"detail": {0: ["Solcast request failed"]}}) == b'{"detail":{"0":[null]}}'
    )


@pytest.mark.parametrize("technical", [False, True])
def test_customer_source_text_is_never_rewritten(technical: bool) -> None:
    value = {"source": "Solcast sheet", "nested": [{"source": "solcast export"}]}
    assert public_source_data(value, technical=technical) == value


def test_operator_provenance_reaches_customers_as_an_allowlist() -> None:
    stored = {
        "capacity_normalization": {
            "policy": "capacity-ratio-v1",
            "factor": 2.0,
            "authorized_by_id": 4,
            "source_binding_id": 5,
            "raw_payload_capacity_kw": 1.0,
        },
        "reference_capacity_normalization": None,
        "reference_import": {
            "id": 1,
            "source_name": "private.json",
            "source_settings": {
                "unit": "kW",
                "source_reference": "private",
                "upload_provenance": {"warnings": [], "file_metadata": {"site": "private"}},
            },
        },
    }
    assert public_source_data(stored) == {
        "capacity_normalization": {"policy": "capacity-ratio-v1", "factor": 2.0},
        "reference_capacity_normalization": None,
        "reference_import": {
            "id": 1,
            "source_settings": {"unit": "kW", "upload_provenance": {"warnings": []}},
        },
    }


@pytest.mark.django_db(transaction=True)
def test_saved_forecast_email_hides_the_provider_in_all_alternatives() -> None:
    delivery = ready()
    assert delivery.snapshot is not None
    account = delivery.snapshot.job.binding.provider_account
    ProviderAccount.objects.filter(pk=account.pk).update(
        provider="solcast",
        external_account_id="private-account",
        encrypted_api_key=encrypt_secret("test-only-solcast-key"),
    )
    account.refresh_from_db()
    message = build_message(delivery)
    assert "solcast" not in message.body.casefold()
    assert "private-account" not in message.body
    assert "solar irradiance forecast (GHI), not electricity generation" in message.body
    for alternative in message.alternatives:
        assert isinstance(alternative[0], str)
        assert "solcast" not in alternative[0].casefold()
    for attachment in message.attachments:
        assert isinstance(attachment, tuple)
        assert isinstance(attachment[1], bytes)
        with ZipFile(BytesIO(attachment[1])) as archive:
            for name in archive.namelist():
                content = archive.read(name).decode("utf-8").casefold()
                assert "solcast" not in content
                assert "private-account" not in content
                assert "test-only-solcast-key" not in content
    assert attached_sheet(message)["B9"].value == "Irradiance (kWh/m²)"
    account.refresh_from_db()
    assert account.provider == "solcast"
    assert account.external_account_id == "private-account"


@pytest.mark.django_db(transaction=True)
def test_calibration_details_hide_nested_provider_identity_and_preserve_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    assert plant.current_configuration is not None
    manifest = {
        "sources": {
            "2026-11-08": {
                "provider": "Solcast",
                "resource_id": "private-site",
                "source_kind": "estimated_actuals",
                "snapshot_id": 71,
            }
        },
        "observations": [{"observation_id": 11, "source": "My Solcast meter note"}],
        "raw_payload": {"private": "Solcast"},
    }
    run = CalibrationRun.objects.create(
        plant=plant,
        configuration=plant.current_configuration,
        binding=system.binding,
        requested_by=system.member,
        window_start=NOW.date(),
        window_end=NOW.date(),
        cutoff_at=NOW,
        dataset_manifest=deepcopy(manifest),
        dataset_sha256="a" * 64,
        result={"quality_flags": ["solcast_unvalidated"], "provisional_production": True},
    )
    client = Client()
    client.force_login(system.member)
    response = client.get(
        f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/plants/{plant.pk}/calibrations/{run.pk}/"
    )
    assert response.status_code == 200
    public = response.json()
    assert public["dataset_manifest"]["sources"]["2026-11-08"] == {
        "source_kind": "estimated_actuals",
        "snapshot_id": 71,
    }
    assert public["dataset_manifest"]["observations"][0]["source"] == "My Solcast meter note"
    assert "raw_payload" not in public["dataset_manifest"]
    assert public["dataset_sha256"] == "a" * 64
    assert public["result"] == {
        "quality_flags": ["reference_unvalidated"],
        "provisional_production": True,
    }
    run.refresh_from_db()
    assert run.dataset_manifest == manifest


@pytest.mark.parametrize("technical", [False, True])
def test_provider_credentials_are_removed_from_nested_public_data(technical: bool) -> None:
    assert public_source_data(
        {
            "name": "Plant",
            "nested": {
                "api_key": "test-only-raw-key",
                "encrypted_api_key": "test-only-ciphertext",
                "secret_reference": "env:OBSOLETE",
                "value": 1,
            },
        },
        technical=technical,
    ) == {"name": "Plant", "nested": {"value": 1}}


def test_provider_accounts_are_not_registered_in_admin() -> None:
    from pyracast.platform_admin.site import site

    assert not site.is_registered(ProviderAccount)
