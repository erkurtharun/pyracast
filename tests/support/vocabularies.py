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
from pyracast.api.serializers import BindingInput
from pyracast.api.session import LanguageInput
from pyracast.change_requests.models import ChangeRequest, IncidentChange, ProductionChangeItem
from pyracast.change_requests.operations import submit_incident, submit_production
from pyracast.change_requests.serializers import IncidentRequestInput
from pyracast.connections.models import ProviderAccount, SiteBinding
from pyracast.connections.operations import create_account
from pyracast.plants.production_changes import ProductionChange
from pyracast.plants.serializers import ProductionChangeRow
from pyracast.regions.models import AuditEvent, Region, RegionLocationChangeRequest
from pyracast.regions.operations import configure_binding, transition_region
from tests.support.change_request_boundaries import (
    incident_request,
    incident_values,
    make_request_case,
    production_request,
)
from tests.support.domain import NOW, make_system, provider_key

OUTSIDE = "xx"
COVERED_APPS = ("accounts", "regions", "connections", "change_requests")

type Row = Callable[[pytest.MonkeyPatch], models.Model]
type Operation = Callable[[pytest.MonkeyPatch], Callable[[str], object]]


@dataclass(frozen=True)
class StoredVocabulary:
    model: type[models.Model]
    field: str
    row: Row
    retired: tuple[str, ...] = ()
    published: bool = True

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
    serializer: type[serializers.Serializer[Any]] | None
    data: dict[str, object]
    field: str
    operation: Operation
    accepted: str
    error: str
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
    StoredVocabulary(ProviderAccount, "provider", _account, published=False),
    StoredVocabulary(SiteBinding, "power_basis", _binding),
    StoredVocabulary(SiteBinding, "health", _binding),
    StoredVocabulary(ChangeRequest, "kind", _production_request),
    StoredVocabulary(ChangeRequest, "status", _production_request),
    StoredVocabulary(ProductionChangeItem, "action", _production_item),
    StoredVocabulary(IncidentChange, "action", _incident_change),
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
        accepted="dc",
        error="An AC or DC power basis is required.",
    ),
    InputVocabulary(
        "region_transition",
        None,
        {"status": "rejected"},
        "status",
        _transition_region,
        accepted="suspended",
        error="Use approval for approval transitions.",
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
)
