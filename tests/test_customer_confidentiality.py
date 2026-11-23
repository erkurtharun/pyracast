import json
from collections.abc import Iterator
from datetime import UTC, timedelta
from io import StringIO
from typing import Any

import pytest
from django.core.management import call_command
from django.test import Client
from django.urls import URLPattern, URLResolver, get_resolver
from django.utils import timezone

from pyracast.connections.models import ProviderAccount, SiteBinding
from pyracast.connections.secrets import encrypt_secret
from pyracast.forecasts.models import (
    CollectionJob,
    ForecastResult,
    ForecastSnapshot,
    RequestAttempt,
)
from pyracast.irradiance import local_day_bounds
from pyracast.plants.models import CalibrationRun
from pyracast.plants.production_changes import ProductionChange, change_production
from tests.support.change_request_boundaries import make_request_case, production_request
from tests.support.domain import NOW, make_system
from tests.support.provisional_measurements import DAY

CUSTOMER_FIELDS = {
    "action",
    "activation_blocker",
    "activation_id",
    "actor_id",
    "added",
    "address",
    "address_roles",
    "addresses",
    "aggregated",
    "applied_observation_id",
    "applied_revision_id",
    "archived_days",
    "as_of",
    "assessment",
    "attempts",
    "attention",
    "authorized",
    "automatic",
    "available_at",
    "available_count",
    "available_dc_mw",
    "available_end_date",
    "available_export_mw",
    "available_inverter_mw",
    "available_start_date",
    "azimuth_degrees",
    "base_revision",
    "baseline_energy_mwh",
    "baseline_metrics",
    "baseline_source_kind",
    "before",
    "bias_mw",
    "binding_id",
    "binding_revision",
    "calibration_id",
    "calibration_in_sample",
    "can_admin",
    "can_edit",
    "can_propose",
    "can_propose_revert",
    "configuration_current",
    "current_calibration",
    "disabled_cause",
    "restore_blocker",
    "can_revert",
    "can_review",
    "can_withdraw",
    "capacity_basis",
    "capacity_kw",
    "capacity_semantics_verified",
    "changes",
    "closed_at",
    "code",
    "collected_at",
    "comparison",
    "complete",
    "complete_through",
    "completed_at",
    "configuration",
    "configuration_created_at",
    "configuration_id",
    "configuration_revision",
    "confirm_implausible",
    "conflict",
    "connected",
    "content_sha256",
    "corrected",
    "count",
    "coverage_fraction",
    "coverage_seconds",
    "created",
    "created_at",
    "csrf_token",
    "current",
    "current_configuration",
    "current_energy_mwh",
    "cutoff_at",
    "daily_bias_mwh",
    "daily_mae_mwh",
    "daily_reminder_limit",
    "data_source",
    "dataset_manifest",
    "dataset_sha256",
    "date",
    "day",
    "daylight",
    "daylight_only",
    "daylight_policy",
    "daylight_status",
    "daylight_windows",
    "days",
    "dc_capacity_kw",
    "dc_capacity_mw",
    "decision_reason",
    "declared_configuration_id",
    "deleted_at",
    "deliveries",
    "disconnected_at",
    "delivery_status",
    "duration_seconds",
    "edit_blocked_reason",
    "edit_unavailable_reason",
    "effective",
    "effective_from",
    "efficiency_factor_percent",
    "eic",
    "eligible",
    "email",
    "enabled",
    "end",
    "end_date",
    "endpoint",
    "ends_at",
    "energy",
    "energy_kwh_m2",
    "energy_mj_m2",
    "energy_mwh",
    "energy_scope",
    "error_mwh",
    "evaluated_at",
    "evidence_state",
    "excluded_days",
    "existing",
    "expected_revision",
    "expected_revision_id",
    "expires_at",
    "export_limit_mw",
    "factor",
    "failed_days",
    "fields",
    "file_name",
    "file_sha256",
    "first_settlement_date",
    "forecast_complete",
    "forecast_status",
    "former_member",
    "freshness",
    "ghi_wm2",
    "has_usable_password",
    "health",
    "hidden_by_departure",
    "hide_recipients",
    "historical_configuration_assumed",
    "history_local_time",
    "history_requests_reserved_per_day",
    "history_retry_minutes",
    "history_retry_until_local",
    "hours",
    "id",
    "idempotency_key",
    "ids",
    "inactive_reason",
    "incident",
    "incident_action",
    "incident_id",
    "incident_revision_ids",
    "incident_title",
    "incidents",
    "include_workspace_members",
    "input_coverage_fraction",
    "input_coverage_seconds",
    "interval_mae_mw",
    "intervals",
    "inverter_capacity_mw",
    "invitation_id",
    "invitations",
    "inviter",
    "is_reverted",
    "issuance_cutoff",
    "issuance_id",
    "issued_at",
    "item_count",
    "kind",
    "language",
    "language_preference",
    "last_attempt",
    "last_delivery",
    "last_error",
    "last_settlement_date",
    "last_success_at",
    "late",
    "latest_day_status",
    "latest_matched_day",
    "latest_performance_index",
    "latitude",
    "limit_mw",
    "local_date",
    "longitude",
    "manual_observation_ids",
    "marked",
    "matched_days",
    "max_forecast_times",
    "mean_power_mw",
    "measured_energy_mwh",
    "measurement_basis",
    "measurement_policy",
    "member_roles",
    "members",
    "memberships",
    "message",
    "metric_scope",
    "metrics",
    "mfa_required",
    "missing_count",
    "missing_days",
    "model_version",
    "mounting_type",
    "name",
    "native_intervals",
    "new_energy_mwh",
    "next",
    "next_check_at",
    "next_collection_at",
    "notes",
    "observation",
    "observations",
    "observed_zero",
    "origin",
    "outage_scenario",
    "page",
    "parameters",
    "parameters_revision",
    "partial_energy_kwh_m2",
    "partial_energy_mj_m2",
    "partial_energy_mwh",
    "pending_jobs",
    "pending_request_id",
    "pending_review_count",
    "performance_index",
    "performance_ratio",
    "period_end",
    "period_start",
    "placement",
    "plant",
    "plant_id",
    "plant_name",
    "plants_at_recipient_limit",
    "policy",
    "power_basis",
    "predicted_energy_mwh",
    "preview",
    "previous",
    "previous_configuration_observations",
    "production_diagnostic",
    "production_items",
    "proposal_unavailable_reason",
    "proposed",
    "proposed_by",
    "provenance",
    "provider",
    "provider_intervals",
    "provider_originals",
    "provider_plant_id",
    "provisional",
    "provisional_production",
    "provisional_snapshot_ids",
    "published_at",
    "purge_after",
    "quality",
    "quality_flags",
    "reason",
    "recent_jobs",
    "recipient_count",
    "recipient_limit",
    "recipient_limit_exceeded",
    "recipient_roles",
    "recipients",
    "recorded_by_id",
    "recorded_count",
    "reference_capacity_normalization",
    "reference_kind",
    "reference_provenance",
    "region_id",
    "regional_irradiance",
    "relative_error",
    "reminder",
    "request_id",
    "requested_by_id",
    "requested_days",
    "requests_today",
    "resolution_minutes",
    "result",
    "results",
    "retry_after",
    "retry_until",
    "review_fingerprint",
    "review_reason",
    "reviewed_at",
    "reviewed_by",
    "revision",
    "revision_id",
    "role",
    "scheduled_for",
    "scope",
    "scored_hours",
    "scored_intervals",
    "selection_cutoff",
    "selection_policy",
    "selection_state",
    "sent_at",
    "session_tag",
    "settlement_plant_id",
    "settlement_snapshot_ids",
    "skill_days",
    "skill_score",
    "slot_scope",
    "snapshot_id",
    "source",
    "source_age_seconds",
    "source_kind",
    "source_kinds",
    "source_positive_mae_mw",
    "source_snapshot_ids",
    "source_snapshots",
    "stale",
    "stale_reason",
    "start",
    "start_date",
    "starts_at",
    "state",
    "status",
    "sunrise",
    "sunset",
    "supersedes_id",
    "surface",
    "surface_azimuth_degrees",
    "surface_tilt_degrees",
    "tenant_id",
    "tilt_degrees",
    "times",
    "timezone",
    "title",
    "token",
    "total_mwh",
    "unavailable_reason",
    "uncertainty",
    "unit",
    "unread",
    "unread_outcome_count",
    "updated_at",
    "user",
    "user_id",
    "username",
    "valid_coverage_seconds",
    "values",
    "vintage",
    "warnings",
    "window_end",
    "window_start",
    "windows",
    "workspace",
    "workspace_member_count",
    "workspace_recipients",
}
PRIVATE_VALUES = (
    "private-account-k05",
    "private-site-k05",
    "private-authorization-k05",
    "private-key-k05",
    "private-upload-k05",
    "private-file-label-k05",
    "private-reference-k05",
)
PRIVATE_KEYS = {
    "api_key",
    "authorization_reference",
    "authorized_by_id",
    "encrypted_api_key",
    "external_account_id",
    "file_metadata",
    "provider_account_id",
    "raw_payload",
    "raw_payload_capacity_kw",
    "site_id",
    "source_binding_id",
    "source_name",
    "source_reference",
}
QUERIES = {
    "api/tenants/<int:tenant_id>/regions/<int:region_id>/forecast/": "?date={day}",
    "api/tenants/<int:tenant_id>/regions/<int:region_id>/plants/<int:plant_id>/forecast/": (
        "?date={day}"
    ),
}


def _schema_fields(node: Any) -> Iterator[str]:
    if isinstance(node, dict):
        yield from node.get("properties", {})
        for value in node.values():
            yield from _schema_fields(value)
    elif isinstance(node, list):
        for value in node:
            yield from _schema_fields(value)


def _keys(node: Any) -> Iterator[str]:
    if isinstance(node, dict):
        for key, value in node.items():
            yield str(key)
            yield from _keys(value)
    elif isinstance(node, list):
        for value in node:
            yield from _keys(value)


def _get_routes(patterns: list[URLPattern | URLResolver], prefix: str = "") -> Iterator[str]:
    for pattern in patterns:
        route = prefix + str(pattern.pattern)
        if isinstance(pattern, URLResolver):
            yield from _get_routes(pattern.url_patterns, route)
        elif route.startswith(("api/session/", "api/tenants/")) and hasattr(
            getattr(pattern.callback, "cls", None), "get"
        ):
            yield route


def test_customer_schema_fields_are_an_allowlist() -> None:
    output = StringIO()
    call_command("spectacular", format="openapi-json", stdout=output)
    schema = json.loads(output.getvalue())
    fields = set(_schema_fields(schema["components"]))
    for operations in schema["paths"].values():
        for operation in operations.values():
            fields.update(parameter["name"] for parameter in operation.get("parameters", ()))
    assert fields == CUSTOMER_FIELDS
    assert "solcast" not in json.dumps(schema).casefold()


def _stored_reference_manifest(binding_id: int, administrator_id: int) -> dict[str, Any]:
    return {
        "snapshot_id": 71,
        "binding_id": binding_id,
        "source_kind": "estimated_actuals",
        "source_product": "historical_rooftop_pv",
        "capacity_normalization": _stored_normalization(administrator_id),
        "reference_import": {
            "id": 7,
            "sha256": "b" * 64,
            "source_name": "private-upload-k05.json",
            "imported_at": NOW.isoformat(),
            "source_settings": {
                "product": "historical_rooftop_pv",
                "settings_matched": True,
                "unit": "kW",
                "timestamp_convention": "period_end",
                "period": "PT30M",
                "source_reference": "private-reference-k05",
                "upload_provenance": {
                    "transport": "json",
                    "file_metadata": {"site_label": "private-file-label-k05"},
                    "file_verified_fields": ["capacity"],
                    "operator_declared_fields": ["azimuth"],
                    "warnings": ["timezone_assumed"],
                    "binding_id": binding_id,
                },
            },
        },
    }


def _stored_normalization(administrator_id: int) -> dict[str, Any]:
    return {
        "id": 3,
        "policy": "capacity-ratio-v1",
        "source_snapshot_id": 11,
        "source_binding_id": 12,
        "source_capacity_kw": 1.0,
        "source_available_at": None,
        "target_capacity_kw": 2.0,
        "factor": 2.0,
        "authorized_by_id": administrator_id,
        "authorization_reference": "private-authorization-k05",
        "normalized_at": NOW.isoformat(),
        "raw_payload_capacity_kw": 1.0,
    }


@pytest.mark.django_db(transaction=True)
def test_every_customer_get_keeps_provider_internals_private(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = make_request_case(monkeypatch)
    system, plant = case.system, case.plant
    request = production_request(case)
    ProviderAccount.objects.filter(pk=system.account.pk).update(
        provider="solcast",
        external_account_id="private-account-k05",
        authorization_reference="private-authorization-k05",
        encrypted_api_key=encrypt_secret("private-key-k05"),
    )
    SiteBinding.objects.filter(pk=system.binding.pk).update(site_id="private-site-k05")
    job = CollectionJob.objects.filter(binding=system.binding).earliest("pk")
    snapshot = ForecastSnapshot.objects.create(
        job=job,
        binding=system.binding,
        raw_payload={"resource_id": "private-site-k05"},
        normalized_intervals=[],
        collected_at=NOW,
        source_kind="forecast",
    )
    ForecastResult.objects.create(
        snapshot=snapshot,
        payload={
            "energy_kwh_m2": None,
            "complete": False,
            "quality_flags": ["unvalidated"],
            "reference_capacity_normalization": _stored_normalization(system.admin.pk),
        },
        published_at=NOW,
    )
    assert plant.current_configuration is not None
    run = CalibrationRun.objects.create(
        plant=plant,
        configuration=plant.current_configuration,
        binding=system.binding,
        requested_by=system.member,
        window_start=NOW.date(),
        window_end=NOW.date(),
        cutoff_at=NOW,
        dataset_manifest={
            "sources": {
                "2026-11-08": _stored_reference_manifest(system.binding.pk, system.admin.pk)
            }
        },
        dataset_sha256="a" * 64,
        result={"quality_flags": ["unvalidated"]},
    )
    values = {
        "tenant_id": system.tenant.pk,
        "region_id": system.region.pk,
        "plant_id": plant.pk,
        "request_id": request.pk,
        "calibration_id": run.pk,
    }
    client = Client()
    client.force_login(system.member)
    routes = sorted(_get_routes(get_resolver().url_patterns))
    assert len(routes) >= 20
    for route in routes:
        path = "/" + route
        for name, value in values.items():
            path = path.replace(f"<int:{name}>", str(value))
        path += QUERIES.get(route, "").format(day=job.target_date)
        response = client.get(path)
        assert response.status_code == 200, (route, response.content)
        content = response.content.decode()
        assert "solcast" not in content.casefold(), route
        assert not [value for value in PRIVATE_VALUES if value in content], route
        assert not PRIVATE_KEYS & set(_keys(response.json())), route
    calibration = client.get(
        f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/plants/{plant.pk}"
        f"/calibrations/{run.pk}/"
    ).json()
    reference = calibration["dataset_manifest"]["sources"]["2026-11-08"]
    assert reference["reference_import"] == {
        "id": 7,
        "sha256": "b" * 64,
        "imported_at": NOW.isoformat(),
        "source_settings": {
            "product": "historical_rooftop_pv",
            "settings_matched": True,
            "unit": "kW",
            "timestamp_convention": "period_end",
            "period": "PT30M",
            "upload_provenance": {
                "file_verified_fields": ["capacity"],
                "operator_declared_fields": ["azimuth"],
                "warnings": ["timezone_assumed"],
            },
        },
    }
    assert reference["capacity_normalization"] == {
        "policy": "capacity-ratio-v1",
        "source_capacity_kw": 1.0,
        "target_capacity_kw": 2.0,
        "factor": 2.0,
        "normalized_at": NOW.isoformat(),
    }
    run.refresh_from_db()
    assert run.dataset_manifest["sources"]["2026-11-08"]["reference_import"]["source_name"] == (
        "private-upload-k05.json"
    )


@pytest.mark.django_db(transaction=True)
def test_production_history_returns_customer_source_text_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = make_request_case(monkeypatch)
    system, plant = case.system, case.plant
    start, _ = local_day_bounds(DAY, system.binding.timezone)
    assert plant.current_configuration_id is not None
    change_production(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        configuration_id=plant.current_configuration_id,
        unit="MWh",
        changes=[
            ProductionChange(
                start, start + timedelta(hours=1), None, "set", 2, source="Solcast sheet"
            )
        ],
    )
    client = Client()
    client.force_login(system.member)
    response = client.get(
        f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/plants/{plant.pk}/production/",
        {"start_date": DAY.isoformat(), "end_date": DAY.isoformat()},
    )
    assert response.status_code == 200
    [interval] = [row for row in response.json()["intervals"] if row["effective"]]
    assert interval["effective"]["source"] == "Solcast sheet"
    assert interval["observation"]["source"] == "Solcast sheet"


@pytest.mark.django_db(transaction=True)
def test_collection_schedule_reports_only_the_plants_own_collection() -> None:
    system = make_system(approved=True)
    client = Client()
    client.force_login(system.member)
    path = f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/collection-schedule/"
    before = client.get(path).json()
    now = timezone.now()
    RequestAttempt.objects.create(
        provider_account=system.account,
        utc_day=now.astimezone(UTC).date(),
        created_at=now,
        purpose="forecast",
        outcome="succeeded",
    )
    after = client.get(path).json()
    assert {**after, "effective_from": None} == {**before, "effective_from": None}
    assert not {
        "daily_request_limit",
        "used_today",
        "remaining_today",
        "quota_utc_day",
        "requests_by_outcome",
    } & set(after)
    assert set(after) >= {"recent_jobs", "last_success_at", "next_collection_at"}
