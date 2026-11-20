import itertools
import time
from datetime import timedelta

import httpx
import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse

from pyracast.connections.contracts import ProviderError
from pyracast.connections.models import BindingHealth, SiteBinding
from pyracast.connections.operations import validate_binding
from pyracast.connections.secrets import encrypt_secret
from pyracast.forecasts.models import RequestAttempt, RequestOutcome
from pyracast.providers import SolcastProvider
from pyracast.regions.operations import configure_binding
from tests.support.admin_setup import admin_system, setup_input
from tests.support.api import verified_admin
from tests.support.domain import NOW, FakeAdapter, System, make_system

pytestmark = pytest.mark.django_db(transaction=True)

RESOURCE_ID = "Use only letters, digits, hyphens and underscores."


def supersede(system: System) -> SiteBinding:
    return configure_binding(
        system.admin,
        system.region.pk,
        provider_account_id=system.account.pk,
        site_id="moved-site",
        capacity_kw=1,
        capacity_semantics_verified=True,
    )


def health(system: System) -> str:
    system.binding.refresh_from_db()
    return system.binding.health


def test_validating_a_superseded_binding_spends_no_provider_request() -> None:
    system = make_system()
    supersede(system)
    calls: list[int] = []

    with pytest.raises(ValidationError):
        validate_binding(system.admin, system.binding.pk, FakeAdapter(lambda: calls.append(1)), NOW)

    assert calls == []
    assert not RequestAttempt.objects.exists()


def test_a_binding_superseded_while_the_provider_answers_keeps_its_answered_attempt() -> None:
    system = make_system()

    def answer_after_a_new_setup() -> None:
        supersede(system)

    with pytest.raises(ValidationError):
        validate_binding(
            system.admin, system.binding.pk, FakeAdapter(answer_after_a_new_setup), NOW
        )

    assert RequestAttempt.objects.get().outcome == RequestOutcome.SUCCEEDED
    assert SiteBinding.objects.get(pk=system.binding.pk).validated_at is None


def test_a_provider_timeout_keeps_the_stored_health() -> None:
    system = make_system()
    validate_binding(system.admin, system.binding.pk, FakeAdapter(), NOW)

    def time_out() -> None:
        raise ProviderError("provider_request_failed")

    with pytest.raises(ProviderError):
        validate_binding(
            system.admin, system.binding.pk, FakeAdapter(time_out), NOW + timedelta(minutes=1)
        )

    assert health(system) == BindingHealth.HEALTHY
    assert RequestAttempt.objects.filter(outcome=RequestOutcome.FAILED).count() == 1


@pytest.mark.parametrize(
    ("code", "status_code"),
    [("provider_request_failed", 401), ("provider_request_failed", 404), ("invalid_site_id", None)],
)
def test_a_refused_validation_marks_the_binding_unhealthy(
    code: str, status_code: int | None
) -> None:
    system = make_system()
    validate_binding(system.admin, system.binding.pk, FakeAdapter(), NOW)

    def refuse() -> None:
        raise ProviderError(code, retryable=False, status_code=status_code)

    with pytest.raises(ProviderError):
        validate_binding(
            system.admin, system.binding.pk, FakeAdapter(refuse), NOW + timedelta(minutes=1)
        )

    assert health(system) == BindingHealth.ERROR


def test_a_resource_id_the_provider_does_not_use_is_refused_at_setup() -> None:
    system = admin_system()
    client, _ = verified_admin()

    response = client.post(
        reverse("admin:workspace_plant", args=(system.region.pk,)),
        setup_input(system, site_id="abc.def"),
    )

    assert response.status_code == 400
    assert RESOURCE_ID in response.content.decode()
    assert SiteBinding.objects.filter(region=system.region).count() == 1


def test_a_provider_answer_slower_than_the_request_deadline_fails_as_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = admin_system()
    system.account.encrypted_api_key = encrypt_secret("slow-answer-key")
    system.account.save(update_fields=("encrypted_api_key",))
    binding = SiteBinding.objects.select_related("provider_account").get(pk=system.binding.pk)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"forecasts": []}))
    ticks = itertools.count(0, 100)
    monkeypatch.setattr(time, "monotonic", lambda: float(next(ticks)))

    with pytest.raises(ProviderError) as raised:
        SolcastProvider(transport, clock=lambda: NOW).validate(binding)

    assert raised.value.code == "provider_request_failed"
    assert raised.value.retryable
