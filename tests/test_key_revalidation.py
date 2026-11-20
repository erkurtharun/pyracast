from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from django.utils import timezone
from pyracast.connections.revalidation import (
    RevalidationOutcome,
    claim_due_revalidations,
    dispatch_revalidations,
    revalidate_account,
)

from pyracast.connections import operations, revalidation
from pyracast.connections.contracts import ProviderAdapter, ProviderError
from pyracast.connections.models import ProviderAccount, SiteBinding
from pyracast.forecasts.models import RequestAttempt
from pyracast.regions.models import AuditEvent, Region
from tests.support.domain import NOW, FakeAdapter, System, make_system, provider_key

pytestmark = pytest.mark.django_db(transaction=True)
NEXT_UTC_DAY = datetime.combine(NOW.date() + timedelta(days=1), datetime.min.time(), UTC)


def second_plant(system: System) -> SiteBinding:
    region = Region.objects.create(
        tenant=system.tenant,
        name="Second site",
        latitude=41,
        longitude=30,
        timezone="Europe/Istanbul",
    )
    binding = SiteBinding.objects.create(
        region=region,
        provider_account=system.account,
        region_revision=1,
        site_id="second-site",
        capacity_kw=1,
        latitude=41,
        longitude=30,
        timezone=region.timezone,
        validated_at=NOW - timedelta(days=1),
        validated_credential_revision=1,
        capacity_semantics_verified=True,
    )
    Region.objects.filter(pk=region.pk).update(status="approved", approved_binding=binding)
    return binding


def replaced(system: System) -> ProviderAccount:
    operations.replace_api_key(
        system.admin, system.account.pk, provider_key("NEW"), expected_revision=1
    )
    return ProviderAccount.objects.get(pk=system.account.pk)


def adapters(adapter: ProviderAdapter) -> MagicMock:
    return MagicMock(return_value=adapter)


def test_a_key_replacement_queues_the_accounts_plants_once() -> None:
    system = make_system(approved=True)
    account = replaced(system)

    assert account.revalidation_due_at is not None
    assert claim_due_revalidations(timezone.now()) == [account.pk]
    assert claim_due_revalidations(timezone.now()) == []
    assert claim_due_revalidations(timezone.now() + revalidation.LEASE) == [account.pk]


def test_revalidation_validates_every_stale_plant_as_the_replacing_operator() -> None:
    system = make_system(approved=True)
    second = second_plant(system)
    replaced(system)

    outcome = revalidate_account(system.account.pk, NOW, adapter_for=adapters(FakeAdapter()))

    assert outcome == RevalidationOutcome.DONE
    for binding in (system.binding, second):
        binding.refresh_from_db()
        assert binding.validated_credential_revision == 2
    assert ProviderAccount.objects.get(pk=system.account.pk).revalidation_due_at is None
    validated = AuditEvent.objects.filter(action="connection_validated", revision__gte=1)
    assert set(validated.values_list("actor_id", flat=True)) == {system.admin.pk}


def test_revalidation_keeps_the_forecast_reserve_and_continues_the_next_utc_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = make_system(approved=True)
    replaced(system)
    ProviderAccount.objects.filter(pk=system.account.pk).update(daily_quota=2)
    monkeypatch.setattr(operations, "remaining_forecast_requests", lambda account, now: 1)
    attempts = RequestAttempt.objects.count()

    outcome = revalidate_account(system.account.pk, NOW, adapter_for=adapters(FakeAdapter()))

    assert outcome == RevalidationOutcome.WAITING
    account = ProviderAccount.objects.get(pk=system.account.pk)
    assert account.revalidation_due_at == NEXT_UTC_DAY
    assert RequestAttempt.objects.count() == attempts
    system.binding.refresh_from_db()
    assert system.binding.validated_credential_revision == 1


def test_a_rejected_new_key_stops_revalidation_of_the_other_plants() -> None:
    system = make_system(approved=True)
    second_plant(system)
    replaced(system)

    def rejected() -> None:
        raise ProviderError("provider_request_failed", retryable=False, status_code=401)

    outcome = revalidate_account(
        system.account.pk, NOW, adapter_for=adapters(FakeAdapter(rejected))
    )

    assert outcome == RevalidationOutcome.KEY_REJECTED
    account = ProviderAccount.objects.get(pk=system.account.pk)
    assert account.authentication_failed_revision == 2
    assert account.revalidation_due_at is None
    assert RequestAttempt.objects.filter(purpose="validation", outcome="failed").count() == 1


def test_a_site_the_new_key_cannot_read_does_not_stop_the_other_plants() -> None:
    system = make_system(approved=True)
    second = second_plant(system)
    replaced(system)
    calls: list[int] = []

    def missing_first_site() -> None:
        calls.append(1)
        if len(calls) == 1:
            raise ProviderError("provider_request_failed", retryable=False, status_code=404)

    outcome = revalidate_account(
        system.account.pk, NOW, adapter_for=adapters(FakeAdapter(missing_first_site))
    )

    assert outcome == RevalidationOutcome.DONE
    second.refresh_from_db()
    system.binding.refresh_from_db()
    assert second.validated_credential_revision == 2
    assert (system.binding.health, system.binding.validated_credential_revision) == ("error", 1)


def test_an_unreachable_provider_retries_revalidation_later() -> None:
    system = make_system(approved=True)
    replaced(system)

    def unreachable() -> None:
        raise ProviderError("provider_request_failed")

    outcome = revalidate_account(
        system.account.pk, NOW, adapter_for=adapters(FakeAdapter(unreachable))
    )

    assert outcome == RevalidationOutcome.WAITING
    due = ProviderAccount.objects.get(pk=system.account.pk).revalidation_due_at
    assert due == NOW + revalidation.RETRY_DELAY


def test_a_provider_delay_postpones_revalidation_until_it_ends() -> None:
    system = make_system(approved=True)
    replaced(system)
    ProviderAccount.objects.filter(pk=system.account.pk).update(
        request_blocked_until=NOW + timedelta(hours=2)
    )

    outcome = revalidate_account(system.account.pk, NOW, adapter_for=adapters(FakeAdapter()))

    assert outcome == RevalidationOutcome.WAITING
    due = ProviderAccount.objects.get(pk=system.account.pk).revalidation_due_at
    assert due == NOW + timedelta(hours=2)


def test_an_operator_who_lost_platform_access_ends_the_revalidation() -> None:
    system = make_system(approved=True)
    replaced(system)
    system.admin.is_superuser = False
    system.admin.save(update_fields=("is_superuser",))

    outcome = revalidate_account(system.account.pk, NOW, adapter_for=adapters(FakeAdapter()))

    assert outcome == RevalidationOutcome.STOPPED
    assert ProviderAccount.objects.get(pk=system.account.pk).revalidation_due_at is None
    assert not RequestAttempt.objects.filter(created_at=NOW).exists()


def test_an_account_without_a_replacement_is_left_alone() -> None:
    system = make_system(approved=True)

    outcome = revalidate_account(system.account.pk, NOW, adapter_for=adapters(FakeAdapter()))

    assert outcome == RevalidationOutcome.DONE
    assert RequestAttempt.objects.filter(created_at=NOW).count() == 0


def test_the_dispatcher_sends_each_due_account_to_the_worker() -> None:
    system = make_system(approved=True)
    replaced(system)
    actor = MagicMock()

    assert dispatch_revalidations(actor) == 1

    actor.send.assert_called_once_with(system.account.pk)
