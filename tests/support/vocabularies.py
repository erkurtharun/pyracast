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

from pyracast.accounts.invitations import invite_workspace_member
from pyracast.accounts.language import save_user_language
from pyracast.accounts.models import (
    Membership,
    OutboundMail,
    Tenant,
    UserLanguagePreference,
    WorkspaceInvitation,
)
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
from pyracast.forecasts.quota import reserve_account_request
from pyracast.incidents.api import IncidentInput
from pyracast.incidents.models import PlantIncident
from pyracast.incidents.operations import save_incident
from pyracast.metering.models import EpiasCollectionJob, EpiasSnapshot
from pyracast.notifications.models import (
    DeliveryRecipient,
    NotificationDelivery,
    PreparedMessageFormat,
    PreparedMessageKind,
    RecipientRole,
    ReminderMemberRole,
    ReminderRecipient,
    ReminderSchedule,
)
from pyracast.notifications.operations import configure_reminders
from pyracast.notifications.serializers import ReminderInput
from pyracast.plants.approval import decide_review
from pyracast.plants.models import (
    CalibrationRun,
    PlantConfiguration,
    PlantEvent,
    PlantSubmission,
    ProductionObservation,
)
from pyracast.plants.operations import create_plant
from pyracast.plants.parameters import PlantParameters
from pyracast.plants.production_changes import (
    ObservationInput,
    ProductionChange,
    import_observations,
)
from pyracast.plants.serializers import ObservationRow, PlantInput, ProductionChangeRow
from pyracast.regions.models import AuditEvent, Region, RegionLocationChangeRequest
from pyracast.regions.operations import configure_binding
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
from tests.support.notifications import chosen, configured, ready
from tests.support.plant_operations import make_plant, saved_actuals, snapshot_for

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
    published: bool = True
    # A vocabulary stored under a key of a JSON column names its enumeration explicitly.
    key: str = ""
    enumeration: type[models.TextChoices] | None = None

    @property
    def id(self) -> str:
        return f"{self.model._meta.label}.{self.field}" + (f".{self.key}" if self.key else "")

    @property
    def lookup(self) -> str:
        return f"{self.field}__{self.key}" if self.key else self.field

    @property
    def choices(self) -> list[tuple[str, Any]]:
        if self.enumeration is not None:
            return list(self.enumeration.choices)
        return list(getattr(self.model._meta.get_field(self.field), "choices", None) or ())

    @property
    def blank(self) -> bool:
        return not self.key and bool(
            getattr(self.model._meta.get_field(self.field), "blank", False)
        )

    def assignment(self, row: models.Model, value: str) -> dict[str, object]:
        if not self.key:
            return {self.field: value}
        return {self.field: {**getattr(row, self.field), self.key: value}}

    @property
    def rejected(self) -> tuple[str, ...]:
        return (OUTSIDE, *self.retired)


@dataclass(frozen=True)
class InputVocabulary:
    id: str
    serializer: type[serializers.Serializer[Any]] | None
    data: dict[str, object]
    field: str
    operation: Operation
    accepted: str
    error: str
    rejected: tuple[str, ...] = (OUTSIDE,)


def derived_check(vocabulary: StoredVocabulary) -> models.CheckConstraint | None:
    values = [value for value, _ in vocabulary.choices]
    if vocabulary.blank:
        values = ["", *values]
    expected = models.Q(**{f"{vocabulary.lookup}__in": values})
    return next(
        (
            constraint
            for constraint in vocabulary.model._meta.constraints
            if isinstance(constraint, models.CheckConstraint) and constraint.condition == expected
        ),
        None,
    )


def assert_database_rejects(row: models.Model, values: dict[str, object]) -> None:
    with pytest.raises(IntegrityError) as raised, transaction.atomic():
        type(row)._default_manager.filter(pk=row.pk).update(**values)
    assert isinstance(raised.value.__cause__, CheckViolation)


def assert_serializer_rejects(case: InputVocabulary, value: str) -> None:
    assert case.serializer is not None
    accepted = case.serializer(data=case.data)
    assert accepted.is_valid(), accepted.errors
    refused = case.serializer(data={**case.data, case.field: value})
    assert not refused.is_valid()
    assert case.field in refused.errors


def assert_operation_rejects(
    case: InputVocabulary, operation: Callable[[str], object], value: str
) -> None:
    with pytest.raises(ValidationError) as raised:
        operation(value)
    assert raised.value.messages == [case.error]
    operation(case.accepted)


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
        User.objects.create(username="platform", is_superuser=True, is_staff=True),
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
        scheduled_for=NOW,
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
        system.member,
        system.tenant.pk,
        system.region.pk,
        PlantParameters(name="Plant", dc_capacity_mw=1),
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


def _reminder_recipient(monkeypatch: pytest.MonkeyPatch) -> models.Model:
    return ReminderRecipient.objects.get(schedule=configured()[1])


def _member_role(monkeypatch: pytest.MonkeyPatch) -> models.Model:
    system, schedule = configured()
    return chosen(schedule, system.member, RecipientRole.TO)


def _delivery_recipient(monkeypatch: pytest.MonkeyPatch) -> models.Model:
    return DeliveryRecipient.objects.get(delivery=ready())


def _outbound_mail(monkeypatch: pytest.MonkeyPatch) -> models.Model:
    system = make_system()
    invite_workspace_member(system.member, system.tenant.pk, "guest@example.com")
    return OutboundMail.objects.get()


def _prepared_delivery(monkeypatch: pytest.MonkeyPatch) -> models.Model:
    delivery = ready()
    delivery.prepared_message = {
        "format": PreparedMessageFormat.PLAIN,
        "subject": "Pyracast forecast unavailable",
        "body": "Tomorrow's saved forecast could not be prepared.",
    }
    delivery.forecast_kind, delivery.prepared_at = PreparedMessageKind.UNAVAILABLE, NOW
    delivery.save(update_fields=("prepared_message", "forecast_kind", "prepared_at"))
    return delivery


STORED_VOCABULARIES = (
    StoredVocabulary(Membership, "role", _membership),
    StoredVocabulary(WorkspaceInvitation, "role", _invitation),
    StoredVocabulary(WorkspaceInvitation, "delivery_status", _invitation, retired=("accepted",)),
    StoredVocabulary(WorkspaceInvitation, "revoked_reason", _invitation, published=False),
    StoredVocabulary(UserLanguagePreference, "language", _language),
    StoredVocabulary(Region, "status", _region),
    StoredVocabulary(RegionLocationChangeRequest, "status", _location_change),
    StoredVocabulary(
        AuditEvent,
        "action",
        _audit_event,
        retired=("approved", "rejected", "suspended", "binding_validated"),
    ),
    StoredVocabulary(ProviderAccount, "provider", _account, published=False),
    StoredVocabulary(SiteBinding, "power_basis", _binding),
    StoredVocabulary(SiteBinding, "health", _binding),
    StoredVocabulary(ChangeRequest, "kind", _production_request),
    StoredVocabulary(ChangeRequest, "status", _production_request),
    StoredVocabulary(ProductionChangeItem, "action", _production_item),
    StoredVocabulary(ProductionChangeItem, "quality", _production_item),
    StoredVocabulary(ProductionChangeItem, "baseline_source_kind", _production_item),
    StoredVocabulary(IncidentChange, "action", _incident_change),
    StoredVocabulary(IncidentChange, "kind", _incident_change),
    StoredVocabulary(DispatcherStatus, "phase", _dispatcher_status),
    StoredVocabulary(CollectionJob, "endpoint", _collection_job),
    StoredVocabulary(CollectionJob, "status", _collection_job),
    StoredVocabulary(CollectionJob, "last_error", _collection_job, retired=("old_worker",)),
    StoredVocabulary(RequestAttempt, "purpose", _request_attempt, retired=("operator",)),
    StoredVocabulary(RequestAttempt, "outcome", _request_attempt),
    StoredVocabulary(ForecastSnapshot, "source_kind", _forecast_snapshot),
    StoredVocabulary(EpiasCollectionJob, "source_kind", _epias_job),
    StoredVocabulary(EpiasCollectionJob, "status", _epias_job, retired=("retry",)),
    StoredVocabulary(EpiasCollectionJob, "last_error", _epias_job, retired=("upstream_error",)),
    StoredVocabulary(EpiasSnapshot, "source_kind", _epias_snapshot),
    StoredVocabulary(PlantSubmission, "measurement_basis", _plant_submission),
    StoredVocabulary(PlantSubmission, "mounting_type", _plant_submission),
    StoredVocabulary(PlantConfiguration, "measurement_basis", _plant_configuration),
    StoredVocabulary(PlantConfiguration, "mounting_type", _plant_configuration),
    StoredVocabulary(ProductionObservation, "quality", _observation),
    StoredVocabulary(CalibrationRun, "status", _calibration_run),
    StoredVocabulary(CalibrationRun, "last_error", _calibration_run, retired=("ValueError",)),
    StoredVocabulary(CalibrationRun, "assessment", _calibration_run),
    StoredVocabulary(PlantEvent, "action", _plant_event),
    StoredVocabulary(PlantIncident, "kind", _plant_incident),
    StoredVocabulary(ReminderSchedule, "language", _reminder_schedule),
    StoredVocabulary(ReminderRecipient, "role", _reminder_recipient),
    StoredVocabulary(ReminderMemberRole, "role", _member_role),
    StoredVocabulary(DeliveryRecipient, "role", _delivery_recipient),
    StoredVocabulary(NotificationDelivery, "status", _notification_delivery),
    StoredVocabulary(NotificationDelivery, "language", _notification_delivery),
    StoredVocabulary(
        NotificationDelivery, "last_error", _notification_delivery, retired=("smtp_failure",)
    ),
    StoredVocabulary(
        NotificationDelivery,
        "prepared_message",
        _prepared_delivery,
        key="format",
        enumeration=PreparedMessageFormat,
    ),
    StoredVocabulary(NotificationDelivery, "forecast_kind", _prepared_delivery),
    StoredVocabulary(OutboundMail, "kind", _outbound_mail, published=False),
    StoredVocabulary(OutboundMail, "status", _outbound_mail, published=False),
    StoredVocabulary(OutboundMail, "language", _outbound_mail),
    StoredVocabulary(OutboundMail, "last_error", _outbound_mail, published=False),
)


def _create_account(monkeypatch: pytest.MonkeyPatch) -> Callable[[str], object]:
    administrator = User.objects.create(username="platform", is_superuser=True, is_staff=True)
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


def _review_decision(monkeypatch: pytest.MonkeyPatch) -> Callable[[str], object]:
    system = make_system()
    return lambda decision: decide_review(
        system.admin, system.region.pk, decision, reason="A review reason."
    )


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


def _submit_change(monkeypatch: pytest.MonkeyPatch) -> Callable[..., object]:
    case = make_request_case(monkeypatch)
    configuration = case.plant.current_configuration_id
    assert configuration is not None
    return lambda **values: submit_production(
        *case.scope(case.proposer),
        configuration_id=configuration,
        unit="kWh",
        changes=[ProductionChange(NOW - timedelta(hours=1), NOW, None, energy=1, **values)],
        reason="Verified meter reading",
        idempotency_key=uuid4(),
    )


def _submit_production(monkeypatch: pytest.MonkeyPatch) -> Callable[[str], object]:
    submit = _submit_change(monkeypatch)
    return lambda action: submit(action=action)


def _submit_production_quality(monkeypatch: pytest.MonkeyPatch) -> Callable[[str], object]:
    submit = _submit_change(monkeypatch)
    return lambda quality: submit(action="set", quality=quality)


def _submit_incident_kind(monkeypatch: pytest.MonkeyPatch) -> Callable[[str], object]:
    case = make_request_case(monkeypatch)
    return lambda kind: submit_incident(
        *case.scope(case.proposer),
        action="create",
        idempotency_key=uuid4(),
        reason="Observed equipment outage",
        values={**incident_values(), "kind": kind},
    )


def _create_plant(monkeypatch: pytest.MonkeyPatch) -> Callable[..., object]:
    system = make_system()
    return lambda **values: create_plant(
        system.member,
        system.tenant.pk,
        system.region.pk,
        PlantParameters(name="Plant", dc_capacity_mw=1, **values),
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
    end = NOW - timedelta(days=1)
    saved_actuals(system, end.date())
    return lambda quality: import_observations(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        configuration_id=configuration,
        unit="MWh",
        observations=[ObservationInput(end - timedelta(hours=1), end, 1, quality, "meter")],
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
        recipients=["ops@example.test"],
        times=["10:00"],
        language=language,
        now=NOW,
    )


def _reserve_request(monkeypatch: pytest.MonkeyPatch) -> Callable[[str], object]:
    account = make_system().account
    return lambda purpose: reserve_account_request(account, NOW, purpose=cast(Any, purpose))


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
        None,
        {"provider": "solcast", "external_account_id": "account", "api_key": "key"},
        "provider",
        _create_account,
        accepted="fixture",
        error="Invalid provider account identity.",
    ),
    InputVocabulary(
        "power_basis",
        None,
        {
            "provider_account_id": 1,
            "site_id": "site",
            "capacity_kw": 1,
            "capacity_semantics_verified": True,
            "installation_date": "2026-01-01",
        },
        "power_basis",
        _configure_binding,
        accepted="dc",
        error="An AC or DC power basis is required.",
    ),
    InputVocabulary(
        "review_decision",
        None,
        {"decision": "suspend"},
        "decision",
        _review_decision,
        accepted="reject",
        error="Choose a review decision.",
        rejected=(OUTSIDE, "approved", "pending"),
    ),
    InputVocabulary(
        "language",
        LanguageInput,
        {"language": "en"},
        "language",
        _save_language,
        accepted="tr",
        error="Choose a supported language.",
    ),
    InputVocabulary(
        "incident_change_action",
        IncidentRequestInput,
        {"action": "create", "idempotency_key": str(uuid4())},
        "action",
        _submit_incident,
        accepted="create",
        error="Unknown incident request action.",
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
        accepted="set",
        error="Unknown production action.",
    ),
    InputVocabulary(
        "production_change_quality",
        ProductionChangeRow,
        {
            "starts_at": "2026-11-09T12:00:00Z",
            "ends_at": "2026-11-09T13:00:00Z",
            "expected_revision_id": None,
            "action": "set",
            "energy": 1,
            "quality": "valid",
        },
        "quality",
        _submit_production_quality,
        accepted="outage",
        error="Unsupported observation quality.",
    ),
    InputVocabulary(
        "incident_change_kind",
        None,
        {},
        "kind",
        _submit_incident_kind,
        accepted="grid",
        error="Unsupported outage kind.",
    ),
    InputVocabulary(
        "measurement_basis",
        PlantInput,
        PLANT_DATA,
        "measurement_basis",
        _measurement_basis,
        accepted="ac_generation",
        error="Unsupported meter basis.",
    ),
    InputVocabulary(
        "mounting_type",
        PlantInput,
        PLANT_DATA,
        "mounting_type",
        _mounting_type,
        accepted="tracking",
        error="Unsupported mounting type.",
    ),
    InputVocabulary(
        "observation_quality",
        ObservationRow,
        OBSERVATION_DATA,
        "quality",
        _import_observation,
        accepted="outage",
        error="Unsupported observation quality.",
    ),
    InputVocabulary(
        "incident_kind",
        IncidentInput,
        {"kind": "inverter", "title": "Inverter outage", "starts_at": "2026-11-09T11:00:00Z"},
        "kind",
        _save_incident,
        accepted="grid",
        error="Unsupported outage kind.",
    ),
    InputVocabulary(
        "reminder_language",
        ReminderInput,
        {
            "enabled": True,
            "include_workspace_members": False,
            "language": "en",
            "recipients": [],
            "times": ["10:00"],
            "expected_revision": 0,
        },
        "language",
        _configure_reminders,
        accepted="tr",
        error="A supported reminder language is required.",
    ),
    InputVocabulary(
        "request_purpose",
        None,
        {},
        "purpose",
        _reserve_request,
        accepted="validation",
        error="Unknown request purpose.",
        rejected=(OUTSIDE, "operator"),
    ),
)

FREE_TEXT = frozenset(
    {
        ("accounts.Tenant", "name"),
        ("accounts.WorkspaceInvitation", "email"),
        ("accounts.WorkspaceInvitation", "token_hash"),
        ("accounts.AuthenticationCacheEntry", "cache_key"),
        ("regions.Region", "name"),
        ("regions.Region", "timezone"),
        ("regions.RegionLocationChangeRequest", "timezone"),
        ("regions.RegionLocationChangeRequest", "reason"),
        ("regions.RegionLocationChangeRequest", "decision_reason"),
        ("connections.ProviderAccount", "external_account_id"),
        ("connections.ProviderAccount", "encrypted_api_key"),
        ("connections.ProviderAccount", "api_key_fingerprint"),
        ("connections.ProviderAccount", "authorization_reference"),
        ("connections.SiteBinding", "site_id"),
        ("connections.SiteBinding", "settings_verification_reference"),
        ("connections.SiteBinding", "timezone"),
        ("change_requests.ChangeRequest", "reason"),
        ("change_requests.ChangeRequest", "payload_sha256"),
        ("change_requests.ChangeRequest", "decision_reason"),
        ("change_requests.ProductionChangeItem", "source"),
        ("change_requests.IncidentChange", "title"),
        ("change_requests.IncidentChange", "notes"),
        ("forecasts.HistoricalReferenceImport", "content_sha256"),
        ("forecasts.HistoricalReferenceImport", "source_name"),
        ("forecasts.ReferenceCapacityNormalization", "authorization_reference"),
        ("metering.EpiasConnection", "eic"),
        ("metering.EpiasConnection", "name"),
        ("metering.EpiasConnectionRevision", "eic"),
        ("metering.EpiasSnapshot", "endpoint"),
        ("metering.EpiasSnapshot", "normalization_policy"),
        ("metering.EpiasSnapshot", "content_sha256"),
        ("plants.Plant", "name"),
        ("plants.ProductionObservation", "source"),
        ("plants.CalibrationRun", "measurement_selection_policy"),
        ("plants.CalibrationRun", "input_sha256"),
        ("plants.CalibrationRun", "dataset_sha256"),
        ("plants.ForecastIssuance", "model_version"),
        ("incidents.PlantIncident", "title"),
        ("incidents.PlantIncident", "notes"),
        ("notifications.OutboundMail", "recipient"),
    }
)
# Exception class names and counted region failures written by the dispatcher: an open set.
FAILURE_CODES = frozenset({("forecasts.DispatcherStatus", "last_error")})
