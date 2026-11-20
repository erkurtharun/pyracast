from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone
from dramatiq.brokers.stub import StubBroker

from pyracast.background import clock as monotonic_clock
from pyracast.background.deadlines import LAST_REQUEST_START
from pyracast.background.failures import PhaseOutcome
from pyracast.connections import operations
from pyracast.connections.contracts import ProviderAdapter, ProviderError
from pyracast.connections.models import ProviderAccount, SiteBinding
from pyracast.forecasts import quota, revalidation
from pyracast.forecasts.models import RequestAttempt
from pyracast.forecasts.revalidation import (
    RevalidationOutcome,
    claim_due_revalidations,
    deliver_revalidation,
    dispatch_revalidations,
    revalidate_account,
)
from pyracast.orchestration import dispatcher as command
from pyracast.orchestration.kinds import REVALIDATION
from pyracast.regions.models import AuditEvent, Region
from tests.support.domain import NOW, FakeAdapter, System, make_system, provider_key
from tests.support.providers import replace_key

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
    monkeypatch.setattr(quota, "remaining_forecast_requests", lambda account, now: 1)
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

    def missing_site() -> None:
        raise ProviderError("provider_request_failed", retryable=False, status_code=404)

    def adapter(binding: SiteBinding) -> ProviderAdapter:
        return FakeAdapter(missing_site if binding.pk == system.binding.pk else lambda: None)

    outcome = revalidate_account(system.account.pk, NOW, adapter_for=adapter)

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
    assert due is not None
    assert (
        NOW + revalidation.RETRY_DELAY
        <= due
        < NOW + revalidation.RETRY_DELAY + timedelta(seconds=5)
    )


def test_a_revalidation_past_its_last_request_start_leaves_the_rest_to_the_next_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = make_system(approved=True)
    second = second_plant(system)
    replaced(system)
    ticks = [0.0]
    monkeypatch.setattr(monotonic_clock, "time", SimpleNamespace(monotonic=lambda: ticks[0]))
    validated: list[int] = []

    def adapter(binding: SiteBinding) -> ProviderAdapter:
        def spend_the_ladder() -> None:
            validated.append(binding.pk)
            ticks[0] += LAST_REQUEST_START.total_seconds()

        return FakeAdapter(spend_the_ladder)

    outcome = revalidate_account(system.account.pk, NOW, adapter_for=adapter)

    assert outcome == RevalidationOutcome.WAITING
    assert len(validated) == 1
    due = NOW + LAST_REQUEST_START
    assert ProviderAccount.objects.get(pk=system.account.pk).revalidation_due_at == due
    assert claim_due_revalidations(due) == [system.account.pk]
    revisions = (
        SiteBinding.objects.filter(pk__in=(system.binding.pk, second.pk))
        .order_by("validated_credential_revision")
        .values_list("validated_credential_revision", flat=True)
    )
    assert list(revisions) == [1, 2]


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


def test_the_worker_revalidates_with_the_configured_provider(
    monkeypatch: pytest.MonkeyPatch, pyracast_logs: pytest.LogCaptureFixture
) -> None:
    system = make_system(approved=True)
    replaced(system)
    monkeypatch.setattr("pyracast.providers.adapter_for", adapters(FakeAdapter()))

    deliver_revalidation(system.account.pk)

    system.binding.refresh_from_db()
    assert system.binding.validated_credential_revision == 2
    assert f"account_id={system.account.pk} outcome=done" in pyracast_logs.text


def test_the_revalidation_actor_listens_on_the_default_queue() -> None:
    actor = REVALIDATION.make_actor(StubBroker())

    assert (actor.actor_name, actor.queue_name) == ("revalidate_provider_account", "default")


@pytest.mark.parametrize(
    ("published", "expected"),
    [(3, PhaseOutcome(published=5, error="")), (ConnectionError("private"), None)],
)
def test_the_collection_phase_reports_revalidations_with_its_jobs(
    monkeypatch: pytest.MonkeyPatch, published: object, expected: PhaseOutcome | None
) -> None:
    monkeypatch.setattr(
        command, "dispatch_collections", MagicMock(return_value=PhaseOutcome(published=2))
    )
    monkeypatch.setattr(command, "dispatch_revalidations", MagicMock(side_effect=[published]))
    monkeypatch.setattr(command, "close_old_connections", MagicMock())

    outcome = command._collection(MagicMock(), MagicMock())

    assert outcome == (expected or PhaseOutcome(published=2, error="ConnectionError"))


def test_without_a_recorded_replacement_nobody_revalidates() -> None:
    system = make_system(approved=True)
    ProviderAccount.objects.filter(pk=system.account.pk).update(
        credential_revision=2, revalidation_due_at=NOW
    )

    outcome = revalidate_account(system.account.pk, NOW, adapter_for=adapters(FakeAdapter()))

    assert outcome == RevalidationOutcome.STOPPED
    assert ProviderAccount.objects.get(pk=system.account.pk).revalidation_due_at is None


def test_a_key_already_rejected_stops_revalidation_before_any_request() -> None:
    system = make_system(approved=True)
    replaced(system)
    ProviderAccount.objects.filter(pk=system.account.pk).update(authentication_failed_revision=2)
    attempts = RequestAttempt.objects.count()

    outcome = revalidate_account(system.account.pk, NOW, adapter_for=adapters(FakeAdapter()))

    assert outcome == RevalidationOutcome.KEY_REJECTED
    assert RequestAttempt.objects.count() == attempts


def test_a_plant_changed_meanwhile_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    system = make_system(approved=True)
    replaced(system)

    def changed(*args: object, **kwargs: object) -> None:
        raise ValidationError("This provider binding is no longer current.")

    monkeypatch.setattr(revalidation, "validate_binding", changed)

    outcome = revalidate_account(system.account.pk, NOW, adapter_for=adapters(FakeAdapter()))

    assert outcome == RevalidationOutcome.DONE


def test_a_replacement_during_revalidation_keeps_its_own_due_time() -> None:
    system = make_system(approved=True)
    replaced(system)

    def replaced_again() -> None:
        replace_key(system.admin, system.account.pk, provider_key("NEWER"))

    revalidate_account(system.account.pk, NOW, adapter_for=adapters(FakeAdapter(replaced_again)))

    account = ProviderAccount.objects.get(pk=system.account.pk)
    assert account.credential_revision == 3
    assert account.revalidation_due_at is not None
