from copy import deepcopy
from typing import Any

import pytest
from django.test import Client
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory

from pyracast.api.public import public_source_data
from pyracast.api.renderers import CustomerJSONRenderer
from pyracast.connections.models import ProviderAccount
from pyracast.connections.secrets import encrypt_secret
from pyracast.forecasts.models import CollectionJob, ForecastResult, ForecastSnapshot
from pyracast.notifications.mail import build_message
from pyracast.plants.models import CalibrationRun
from tests.test_domain import NOW, make_system
from tests.test_notifications import ready
from tests.test_plant_operations import make_plant


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
        ("unknown Solcast metadata", "source_details_unavailable"),
    ],
)
def test_legacy_source_kinds_have_public_aliases(value: str, expected: str) -> None:
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
        {"detail": {"Solcast error": "private", "source": "Solcast failed"}}
    ) == {"detail": {"source": "source_details_unavailable"}}
    assert public_source_data({"result": {"solcast_ghi_wm2": 50, "solcast_kwh_m2": None}}) == {
        "result": {"reference_ghi_wm2": 50, "reference_kwh_m2": None}
    }
    assert public_source_data({"result": {"provider": {"name": "Solcast"}, "name": "Solcast"}}) == {
        "result": {"name": "source_details_unavailable"}
    }
    assert public_source_data({"provider": None}) == {}


def test_renderer_preserves_platform_contract_and_sanitizes_customer_errors() -> None:
    renderer = CustomerJSONRenderer()
    factory = APIRequestFactory()
    data = {"detail": "Solcast authentication failed", "provider_account_id": 42}
    assert renderer.render(data) == b'{"detail":"source_details_unavailable"}'
    assert renderer.render(
        data, renderer_context={"request": Request(factory.get("/api/tenants/1/"))}
    ) == renderer.render(data)
    assert (
        renderer.render(
            data, renderer_context={"request": Request(factory.get("/api/platform/accounts/"))}
        )
        == b'{"detail":"Solcast authentication failed","provider_account_id":42}'
    )
    assert (
        renderer.render({"detail": {0: ["Solcast request failed"]}})
        == b'{"detail":{"0":["source_details_unavailable"]}}'
    )


@pytest.mark.django_db(transaction=True)
def test_saved_forecast_email_hides_weather_provider_in_all_alternatives() -> None:
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
    assert "Dataset kind: forecast" in message.body
    assert "Unvalidated irradiance proxy" in message.body
    for alternative in message.alternatives:
        assert isinstance(alternative[0], str)
        assert "solcast" not in alternative[0].casefold()
    for attachment in message.attachments:
        assert isinstance(attachment, tuple)
        assert isinstance(attachment[1], str)
        assert "solcast" not in attachment[1].casefold()
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


@pytest.mark.django_db(transaction=True)
def test_archived_forecast_hides_provider_identity_without_rewriting_archive() -> None:
    system = make_system(approved=True)
    job = CollectionJob.objects.get(binding=system.binding)
    raw = {"provider": "solcast", "resource_id": "private-resource"}
    snapshot = ForecastSnapshot.objects.create(
        job=job,
        binding=system.binding,
        raw_payload=raw,
        normalized_intervals=[],
        collected_at=NOW,
        source_kind="forecast",
    )
    payload = {
        "energy_kwh_m2": None,
        "partial_energy_kwh_m2": 1.25,
        "complete": False,
        "model_version": "solcast-legacy-proxy-v1",
        "quality_flags": ["unvalidated", "solcast_provisional", "incomplete"],
        "vintage": {
            "provider": "solcast",
            "source_kind": "forecast",
            "provider_account_id": 17,
            "external_account_id": "private-account",
            "site_id": "private-site",
            "resource_id": "private-resource",
            "binding_revision": "7:2",
        },
        "provenance": {
            "source_kind": "estimated_actuals",
            "nested": [{"Solcast": "private", "detail": "Solcast response unavailable"}],
            "source_url": "https://api.solcast.com.au/private-resource",
            "production_provider": "epias",
        },
    }
    saved = ForecastResult.objects.create(
        snapshot=snapshot, payload=deepcopy(payload), published_at=NOW
    )
    client = Client()
    client.force_login(system.member)
    response = client.get(
        f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/forecast/"
        f"?date={job.target_date}"
    )
    assert response.status_code == 200
    public = response.json()["result"]
    assert "solcast" not in response.content.decode().casefold()
    assert "private-" not in response.content.decode()
    assert public["energy_kwh_m2"] is None
    assert public["partial_energy_kwh_m2"] == 1.25
    assert public["quality_flags"] == ["unvalidated", "reference_provisional", "incomplete"]
    assert public["vintage"] == {"source_kind": "forecast", "binding_revision": "7:2"}
    assert public["provenance"]["source_kind"] == "estimated_actuals"
    assert public["provenance"]["production_provider"] == "epias"
    saved.refresh_from_db()
    snapshot.refresh_from_db()
    assert saved.payload == payload
    assert snapshot.raw_payload == raw


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
    from pyracast.admin import site

    assert not site.is_registered(ProviderAccount)
