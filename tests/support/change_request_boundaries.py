from dataclasses import dataclass
from datetime import timedelta
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from django.contrib.auth.models import User

from pyracast.accounts.models import Membership
from pyracast.change_requests.models import ChangeRequest
from pyracast.change_requests.operations import (
    approve_request,
    submit_incident,
    submit_production,
)
from pyracast.change_requests.queries import request_detail
from pyracast.plants.models import Plant
from pyracast.plants.production_changes import ProductionChange
from tests.support.domain import NOW, System
from tests.support.plant_operations import make_plant


@dataclass(frozen=True)
class RequestCase:
    system: System
    plant: Plant
    proposer: User

    def scope(self, actor: User) -> tuple[User, int, int, int]:
        return actor, self.system.tenant.pk, self.system.region.pk, self.plant.pk

    def detail_path(self, request: ChangeRequest) -> str:
        return f"/api/tenants/{self.system.tenant.pk}/change-requests/{request.pk}/"


def make_request_case(monkeypatch: pytest.MonkeyPatch) -> RequestCase:
    system, plant = make_plant(monkeypatch)
    proposer = User.objects.create_user(username="proposer")
    Membership.objects.create(user=proposer, tenant=system.tenant)
    return RequestCase(system, plant, proposer)


def production_request(
    case: RequestCase, *, key: UUID | None = None, energy: float = 1234.567
) -> ChangeRequest:
    assert case.plant.current_configuration_id is not None
    return submit_production(
        *case.scope(case.proposer),
        configuration_id=case.plant.current_configuration_id,
        unit="kWh",
        changes=[ProductionChange(NOW - timedelta(hours=1), NOW, None, "set", energy)],
        reason="Verified meter reading",
        idempotency_key=key or uuid4(),
    )


def incident_values() -> dict[str, Any]:
    return {
        "kind": "inverter",
        "title": "Inverter maintenance",
        "notes": "Reported by the operator",
        "starts_at": NOW - timedelta(hours=2),
        "ends_at": None,
        "available_dc_mw": 65.0,
        "available_inverter_mw": None,
        "available_export_mw": None,
    }


def incident_request(case: RequestCase) -> ChangeRequest:
    return submit_incident(
        *case.scope(case.proposer),
        action="create",
        idempotency_key=uuid4(),
        reason="Observed equipment outage",
        values=incident_values(),
    )


def review_fingerprint(case: RequestCase, request: ChangeRequest) -> str:
    return cast(
        str,
        request_detail(case.system.member, case.system.tenant.pk, request.pk)["review_fingerprint"],
    )


def approve(case: RequestCase, request: ChangeRequest) -> ChangeRequest:
    return approve_request(
        case.system.member,
        case.system.tenant.pk,
        request.pk,
        review_fingerprint=review_fingerprint(case, request),
    )
