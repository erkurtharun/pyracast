from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, cast
from uuid import uuid4

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, models, transaction
from django.utils import timezone
from psycopg.errors import CheckViolation
from rest_framework import serializers

from pyracast.accounts.language import save_user_language
from pyracast.accounts.models import Membership, Tenant, UserLanguagePreference, WorkspaceInvitation
from pyracast.api.serializers import AccountInput, BindingInput, TransitionInput
from pyracast.api.session import LanguageInput
from pyracast.change_requests.models import ChangeRequest, IncidentChange, ProductionChangeItem
from pyracast.change_requests.operations import submit_incident, submit_production
from pyracast.change_requests.serializers import IncidentRequestInput
from pyracast.connections.models import ProviderAccount, SiteBinding
from pyracast.connections.operations import create_account
from pyracast.forecasts.models import (
    CollectionJob,
    DispatcherStatus,
    ForecastSnapshot,
    RequestAttempt,
)
from pyracast.incidents.api import IncidentInput
from pyracast.incidents.models import PlantIncident
from pyracast.incidents.operations import save_incident
from pyracast.metering.models import EpiasCollectionJob, EpiasSnapshot
from pyracast.notifications.models import NotificationDelivery, ReminderSchedule
from pyracast.notifications.operations import configure_reminders
from pyracast.notifications.serializers import ReminderInput
from pyracast.plants.models import (
    CalibrationRun,
    PlantConfiguration,
    PlantEvent,
    PlantSubmission,
    ProductionObservation,
)
from pyracast.plants.operations import ObservationInput, create_plant, import_observations
from pyracast.plants.production_changes import ProductionChange
from pyracast.plants.serializers import ObservationRow, PlantInput, ProductionChangeRow
from pyracast.regions.models import AuditEvent, Region, RegionLocationChangeRequest
from pyracast.regions.operations import configure_binding, transition_region
from tests.support.change_request_boundaries import (
    incident_request,
    incident_values,
    make_request_case,
    production_request,
)
from tests.support.domain import NOW, make_system, provider_key
from tests.support.epias_api import archive_source
from tests.support.incident_calibration import incident
from tests.support.metering import connected
from tests.support.notifications import configured, ready
from tests.support.plant_operations import make_plant, snapshot_for

OUTSIDE = "xx"
COVERED_APPS = (
    "accounts",
    "regions",
    "connections",
    "change_requests",
    "forecasts",
    "metering",
    "plants",
    "incidents",
    "notifications",
)

type Row = Callable[[pytest.MonkeyPatch], models.Model]
type Operation = Callable[[pytest.MonkeyPatch], Callable[[str], object]]


@dataclass(frozen=True)
class StoredVocabulary:
    model: type[models.Model]
    field: str
    row: Row
    retired: tuple[str, ...] = ()

    @property
    def id(self) -> str:
        return f"{self.model._meta.label}.{self.field}"

    @property
    def choices(self) -> list[tuple[str, Any]]:
        return list(getattr(self.model._meta.get_field(self.field), "choices", None) or ())

    @property
    def rejected(self) -> tuple[str, ...]:
        return (OUTSIDE, *self.retired)


@dataclass(frozen=True)
class InputVocabulary:
    id: str
    serializer: type[serializers.Serializer[Any]]
    data: dict[str, object]
    field: str
    operation: Operation
    rejected: tuple[str, ...] = (OUTSIDE,)


def derived_check(vocabulary: StoredVocabulary) -> models.CheckConstraint | None:
    expected = models.Q(**{f"{vocabulary.field}__in": [value for value, _ in vocabulary.choices]})
    return next(
        (
            constraint
            for constraint in vocabulary.model._meta.constraints
            if isinstance(constraint, models.CheckConstraint) and constraint.condition == expected
        ),
        None,
    )


def assert_database_rejects(row: models.Model, field: str, value: str) -> None:
    with pytest.raises(IntegrityError) as raised, transaction.atomic():
        type(row)._default_manager.filter(pk=row.pk).update(**{field: value})
    assert isinstance(raised.value.__cause__, CheckViolation)


def assert_serializer_rejects(case: InputVocabulary, value: str) -> None:
    accepted = case.serializer(data=case.data)
    assert accepted.is_valid(), accepted.errors
    refused = case.serializer(data={**case.data, case.field: value})
    assert not refused.is_valid()
    assert case.field in refused.errors


def assert_operation_rejects(operation: Callable[[str], object], value: str) -> None:
    with pytest.raises(ValidationError):
        operation(value)


def _membership(monkeypatch: pytest.MonkeyPatch) -> models.Model:
    tenant = Tenant.objects.create(name="Vocabulary workspace")
    return Membership.objects.create(tenant=tenant, user=User.objects.create(username="viewer"))


def _invitation(monkeypatch: pytest.MonkeyPatch) -> models.Model:
    tenant = Tenant.objects.create(name="Vocabulary workspace")
    return WorkspaceInvitation.objects.create(
        tenant=tenant,
        email="invitee@example.test",
        token_hash="0" * 64,
        created_by=User.objects.create(username="inviter"),
        expires_at=timezone.now() + timedelta(days=7),
    )


def _language(monkeypatch: pytest.MonkeyPatch) -> models.Model:
    return UserLanguagePreference.objects.create(
        user=User.objects.create(username="reader"), language="en"
    )


def _region(monkeypatch: pytest.MonkeyPatch) -> models.Model:
    return make_system().region


def _location_change(monkeypatch: pytest.MonkeyPatch) -> models.Model:
    system = make_system()
    return RegionLocationChangeRequest.objects.create(
        region=system.region,
        requested_by=system.member,
        base_revision=system.region.revision,
        latitude=41,
        longitude=29,
        timezone="Europe/Istanbul",
    )


def _audit_event(monkeypatch: pytest.MonkeyPatch) -> models.Model:
    return AuditEvent.objects.filter(region=make_system().region).earliest("pk")


def _account(monkeypatch: pytest.MonkeyPatch) -> models.Model:
    return create_account(
        User.objects.create(username="platform", is_superuser=True),
        provider="solcast",
        external_account_id="vocabulary",
        api_key=provider_key(),
        authorization_reference="contract",
    )


def _binding(monkeypatch: pytest.MonkeyPatch) -> models.Model:
    return make_system().binding


def _production_request(monkeypatch: pytest.MonkeyPatch) -> ChangeRequest:
    return production_request(make_request_case(monkeypatch))


def _production_item(monkeypatch: pytest.MonkeyPatch) -> models.Model:
    return ProductionChangeItem.objects.get(request=_production_request(monkeypatch))


def _incident_change(monkeypatch: pytest.MonkeyPatch) -> models.Model:
    return IncidentChange.objects.get(request=incident_request(make_request_case(monkeypatch)))


def _dispatcher_status(monkeypatch: pytest.MonkeyPatch) -> models.Model:
    return DispatcherStatus.objects.create(phase="collection", attempted_at=NOW)


def _collection_job(monkeypatch: pytest.MonkeyPatch) -> models.Model:
    return CollectionJob.objects.create(
        binding=make_system().binding,
        scheduled_slot=NOW,
        target_date=NOW.date(),
        available_at=NOW,
    )


def _request_attempt(monkeypatch: pytest.MonkeyPatch) -> models.Model:
    system = make_system()
    return RequestAttempt.objects.create(
        provider_account=system.account,
        binding=system.binding,
        utc_day=NOW.date(),
        created_at=NOW,
        purpose="validation",
    )


def _forecast_snapshot(monkeypatch: pytest.MonkeyPatch) -> models.Model:
    return snapshot_for(make_system(), NOW.date())


def _epias_snapshot(monkeypatch: pytest.MonkeyPatch) -> EpiasSnapshot:
    return archive_source(connected(monkeypatch)[2], "realtime_generation", {0: "1"})


def _epias_job(monkeypatch: pytest.MonkeyPatch) -> models.Model:
    return _epias_snapshot(monkeypatch).job


def _plant_submission(monkeypatch: pytest.MonkeyPatch) -> models.Model:
    system = make_system()
    plant = create_plant(
        system.member, system.tenant.pk, system.region.pk, name="Plant", dc_capacity_mw=1
    )
    return PlantSubmission.objects.get(plant=plant)


def _plant_configuration(monkeypatch: pytest.MonkeyPatch) -> models.Model:
    return PlantConfiguration.objects.get(plant=make_plant(monkeypatch)[1])


def _observation(monkeypatch: pytest.MonkeyPatch) -> models.Model:
    system, plant = make_plant(monkeypatch)
    return ProductionObservation.objects.create(
        plant=plant,
        configuration=PlantConfiguration.objects.get(plant=plant),
        starts_at=NOW - timedelta(hours=1),
        ends_at=NOW,
        energy_mwh=1,
        source="meter",
        recorded_by=system.member,
    )


def _calibration_run(monkeypatch: pytest.MonkeyPatch) -> models.Model:
    system, plant = make_plant(monkeypatch)
    return CalibrationRun.objects.create(
        plant=plant,
        configuration=PlantConfiguration.objects.get(plant=plant),
        binding=system.binding,
        requested_by=system.member,
        window_start=NOW.date() - timedelta(days=7),
        window_end=NOW.date() - timedelta(days=1),
        cutoff_at=NOW,
    )


def _plant_event(monkeypatch: pytest.MonkeyPatch) -> models.Model:
    return PlantEvent.objects.filter(plant=make_plant(monkeypatch)[1]).earliest("pk")


def _plant_incident(monkeypatch: pytest.MonkeyPatch) -> models.Model:
    system, plant = make_plant(monkeypatch)
    return incident(system, plant, NOW - timedelta(hours=2), None)


def _reminder_schedule(monkeypatch: pytest.MonkeyPatch) -> models.Model:
    return configured()[1]


def _notification_delivery(monkeypatch: pytest.MonkeyPatch) -> models.Model:
    return ready()


STORED_VOCABULARIES = (
    StoredVocabulary(Membership, "role", _membership),
    StoredVocabulary(WorkspaceInvitation, "delivery_status", _invitation, retired=("accepted",)),
    StoredVocabulary(UserLanguagePreference, "language", _language),
    StoredVocabulary(Region, "status", _region),
    StoredVocabulary(RegionLocationChangeRequest, "status", _location_change),
    StoredVocabulary(
        AuditEvent,
        "action",
        _audit_event,
        retired=("approved", "rejected", "suspended", "binding_validated"),
    ),
    StoredVocabulary(ProviderAccount, "provider", _account),
    StoredVocabulary(SiteBinding, "power_basis", _binding),
    StoredVocabulary(SiteBinding, "health", _binding),
    StoredVocabulary(ChangeRequest, "kind", _production_request),
    StoredVocabulary(ChangeRequest, "status", _production_request),
    StoredVocabulary(ProductionChangeItem, "action", _production_item),
    StoredVocabulary(IncidentChange, "action", _incident_change),
    StoredVocabulary(DispatcherStatus, "phase", _dispatcher_status),
    StoredVocabulary(CollectionJob, "endpoint", _collection_job),
    StoredVocabulary(CollectionJob, "status", _collection_job),
    StoredVocabulary(RequestAttempt, "purpose", _request_attempt, retired=("operator",)),
    StoredVocabulary(RequestAttempt, "outcome", _request_attempt),
    StoredVocabulary(ForecastSnapshot, "source_kind", _forecast_snapshot),
    StoredVocabulary(EpiasCollectionJob, "source_kind", _epias_job),
    StoredVocabulary(EpiasCollectionJob, "status", _epias_job, retired=("retry",)),
    StoredVocabulary(EpiasSnapshot, "source_kind", _epias_snapshot),
    StoredVocabulary(PlantSubmission, "measurement_basis", _plant_submission),
    StoredVocabulary(PlantSubmission, "mounting_type", _plant_submission),
    StoredVocabulary(PlantConfiguration, "measurement_basis", _plant_configuration),
    StoredVocabulary(PlantConfiguration, "mounting_type", _plant_configuration),
    StoredVocabulary(ProductionObservation, "quality", _observation),
    StoredVocabulary(CalibrationRun, "status", _calibration_run),
    StoredVocabulary(PlantEvent, "action", _plant_event),
    StoredVocabulary(PlantIncident, "kind", _plant_incident),
    StoredVocabulary(ReminderSchedule, "language", _reminder_schedule),
    StoredVocabulary(NotificationDelivery, "status", _notification_delivery),
    StoredVocabulary(NotificationDelivery, "language", _notification_delivery),
)


def _create_account(monkeypatch: pytest.MonkeyPatch) -> Callable[[str], object]:
    administrator = User.objects.create(username="platform", is_superuser=True)
    return lambda provider: create_account(
        administrator,
        provider=provider,
        external_account_id="vocabulary",
        authorization_reference="contract",
    )


def _configure_binding(monkeypatch: pytest.MonkeyPatch) -> Callable[[str], object]:
    system = make_system()
    return lambda basis: configure_binding(
        system.admin,
        system.region.pk,
        provider_account_id=system.account.pk,
        site_id="other-site",
        capacity_kw=2,
        power_basis=basis,
        capacity_semantics_verified=True,
    )


def _transition_region(monkeypatch: pytest.MonkeyPatch) -> Callable[[str], object]:
    system = make_system()
    return lambda status: transition_region(system.admin, system.region.pk, status)


def _save_language(monkeypatch: pytest.MonkeyPatch) -> Callable[[str], object]:
    user = User.objects.create(username="reader")
    return lambda language: save_user_language(user, cast(Any, language))


def _submit_incident(monkeypatch: pytest.MonkeyPatch) -> Callable[[str], object]:
    case = make_request_case(monkeypatch)
    return lambda action: submit_incident(
        *case.scope(case.proposer),
        action=action,
        idempotency_key=uuid4(),
        reason="Observed equipment outage",
        values=incident_values(),
    )


def _submit_production(monkeypatch: pytest.MonkeyPatch) -> Callable[[str], object]:
    case = make_request_case(monkeypatch)
    configuration = case.plant.current_configuration_id
    assert configuration is not None
    return lambda action: submit_production(
        *case.scope(case.proposer),
        configuration_id=configuration,
        unit="kWh",
        changes=[ProductionChange(NOW - timedelta(hours=1), NOW, None, action, 1)],
        reason="Verified meter reading",
        idempotency_key=uuid4(),
    )


def _create_plant(monkeypatch: pytest.MonkeyPatch) -> Callable[..., object]:
    system = make_system()
    return lambda **values: create_plant(
        system.member, system.tenant.pk, system.region.pk, name="Plant", dc_capacity_mw=1, **values
    )


def _measurement_basis(monkeypatch: pytest.MonkeyPatch) -> Callable[[str], object]:
    create = _create_plant(monkeypatch)
    return lambda basis: create(measurement_basis=basis)


def _mounting_type(monkeypatch: pytest.MonkeyPatch) -> Callable[[str], object]:
    create = _create_plant(monkeypatch)
    return lambda mounting: create(mounting_type=mounting)


def _import_observation(monkeypatch: pytest.MonkeyPatch) -> Callable[[str], object]:
    system, plant = make_plant(monkeypatch)
    configuration = plant.current_configuration_id
    assert configuration is not None
    return lambda quality: import_observations(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        configuration_id=configuration,
        unit="MWh",
        observations=[ObservationInput(NOW - timedelta(hours=1), NOW, 1, quality, "meter")],
    )


def _save_incident(monkeypatch: pytest.MonkeyPatch) -> Callable[[str], object]:
    system, plant = make_plant(monkeypatch)
    return lambda kind: save_incident(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        **{**incident_values(), "kind": kind},
    )


def _configure_reminders(monkeypatch: pytest.MonkeyPatch) -> Callable[[str], object]:
    system = make_system(approved=True)
    return lambda language: configure_reminders(
        system.member,
        system.tenant.pk,
        system.region.pk,
        enabled=True,
        recipients=[],
        times=["10:00"],
        language=language,
        now=NOW,
    )


PLANT_DATA: dict[str, object] = {"name": "Plant", "dc_capacity_mw": 1}
OBSERVATION_DATA: dict[str, object] = {
    "starts_at": "2026-11-09T12:00:00Z",
    "ends_at": "2026-11-09T13:00:00Z",
    "energy": 1,
    "quality": "valid",
    "source": "meter",
}

INPUT_VOCABULARIES = (
    InputVocabulary(
        "provider",
        AccountInput,
        {"provider": "solcast", "external_account_id": "account", "api_key": "key"},
        "provider",
        _create_account,
    ),
    InputVocabulary(
        "power_basis",
        BindingInput,
        {
            "provider_account_id": 1,
            "site_id": "site",
            "capacity_kw": 1,
            "capacity_semantics_verified": True,
            "installation_date": "2026-01-01",
        },
        "power_basis",
        _configure_binding,
    ),
    InputVocabulary(
        "region_transition",
        TransitionInput,
        {"status": "rejected"},
        "status",
        _transition_region,
        rejected=(OUTSIDE, "approved", "pending"),
    ),
    InputVocabulary("language", LanguageInput, {"language": "en"}, "language", _save_language),
    InputVocabulary(
        "incident_change_action",
        IncidentRequestInput,
        {"action": "create", "idempotency_key": str(uuid4())},
        "action",
        _submit_incident,
    ),
    InputVocabulary(
        "production_change_action",
        ProductionChangeRow,
        {
            "starts_at": "2026-11-09T12:00:00Z",
            "ends_at": "2026-11-09T13:00:00Z",
            "expected_revision_id": None,
            "action": "set",
            "energy": 1,
        },
        "action",
        _submit_production,
    ),
    InputVocabulary(
        "measurement_basis",
        PlantInput,
        PLANT_DATA,
        "measurement_basis",
        _measurement_basis,
    ),
    InputVocabulary("mounting_type", PlantInput, PLANT_DATA, "mounting_type", _mounting_type),
    InputVocabulary(
        "observation_quality", ObservationRow, OBSERVATION_DATA, "quality", _import_observation
    ),
    InputVocabulary(
        "incident_kind",
        IncidentInput,
        {"kind": "inverter", "title": "Inverter outage", "starts_at": "2026-11-09T11:00:00Z"},
        "kind",
        _save_incident,
    ),
    InputVocabulary(
        "reminder_language",
        ReminderInput,
        {"enabled": True, "language": "en", "recipients": [], "times": ["10:00"]},
        "language",
        _configure_reminders,
    ),
)
