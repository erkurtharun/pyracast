from datetime import timedelta
from typing import Any

import pytest
from django.db import IntegrityError, transaction
from django.db.models import F

from pyracast.change_requests.models import ChangeRequest, ChangeRequestStatus, IncidentChange
from pyracast.change_requests.operations import reject_request
from pyracast.db import violated_constraint
from tests.support.change_request_boundaries import (
    incident_request,
    make_request_case,
    production_request,
)

pytestmark = pytest.mark.django_db(transaction=True)


def refusal(write: Any) -> str | None:
    with pytest.raises(IntegrityError) as raised, transaction.atomic():
        write()
    return violated_constraint(raised.value)


def test_a_rejection_keeps_its_reason_and_the_database_refuses_one_without(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = make_request_case(monkeypatch)
    request = production_request(case)
    reject_request(
        case.system.member, case.system.tenant.pk, request.pk, reason="  Meter was replaced  "
    )
    rows = ChangeRequest.objects.filter(pk=request.pk)
    assert rows.values_list("status", "decision_reason").get() == (
        ChangeRequestStatus.REJECTED,
        "Meter was replaced",
    )

    assert refusal(lambda: rows.update(decision_reason="")) == "changerequest_decision_reason_chk"


@pytest.mark.parametrize("offset", [timedelta(0), timedelta(hours=-1)], ids=["equal", "earlier"])
def test_the_database_refuses_an_incident_proposal_that_ends_before_it_starts(
    offset: timedelta, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = make_request_case(monkeypatch)
    rows = IncidentChange.objects.filter(request=incident_request(case))
    assert rows.values_list("ends_at", flat=True).get() is None

    assert (
        refusal(lambda: rows.update(ends_at=F("starts_at") + offset))
        == "incidentchange_ends_at_chk"
    )
    rows.update(ends_at=F("starts_at") + timedelta(minutes=1))
