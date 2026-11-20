import pytest
from django.utils import timezone

from pyracast.connections.contracts import ProviderError
from pyracast.forecasts.validation import validate_binding
from pyracast.regions.models import AuditEvent
from tests.support.domain import FakeAdapter, make_system

pytestmark = pytest.mark.django_db(transaction=True)


def refuse() -> None:
    raise ProviderError("provider_request_failed", retryable=False, status_code=401)


def test_a_failed_validation_writes_one_audit_line_naming_its_binding_and_cause() -> None:
    system = make_system()

    with pytest.raises(ProviderError):
        validate_binding(system.admin, system.binding.pk, FakeAdapter(refuse), timezone.now())

    event = AuditEvent.objects.get(action__startswith="connection_valid")
    assert (event.action, event.actor_id, event.region_id, event.revision) == (
        "connection_validation_failed",
        system.admin.pk,
        system.region.pk,
        system.binding.region_revision,
    )
    assert event.details == {
        "binding_id": system.binding.pk,
        "error_code": "provider_request_failed",
        "status_code": 401,
    }


def test_a_successful_validation_writes_only_its_own_audit_line() -> None:
    system = make_system()

    validate_binding(system.admin, system.binding.pk, FakeAdapter(), timezone.now())

    assert list(
        AuditEvent.objects.filter(action__startswith="connection_valid").values_list(
            "action", flat=True
        )
    ) == ["connection_validated"]
