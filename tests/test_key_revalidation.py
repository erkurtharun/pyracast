from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from django.core.exceptions import ValidationError
from django.db import connections
from django.utils import timezone
from dramatiq import Message
from dramatiq.brokers.stub import StubBroker

from pyracast.background import clock as monotonic_clock
from pyracast.background.deadlines import LAST_REQUEST_START, QUEUE_DEPTH_LIMIT
from pyracast.background.failures import PhaseOutcome
from pyracast.connections import operations
from pyracast.connections.contracts import ProviderAdapter, ProviderError
from pyracast.connections.models import ProviderAccount, SiteBinding
from pyracast.forecasts import revalidation
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


def due(system: System) -> datetime | None:
    return ProviderAccount.objects.get(pk=system.account.pk).revalidation_due_at


def revalidate(
    system: System,
    adapter_for: Callable[[SiteBinding], ProviderAdapter],
    *,
    claim: datetime | None = None,
) -> RevalidationOutcome:
    lease = claim or due(system)
    assert lease is not None
    return revalidate_account(system.account.pk, NOW, claim=lease, adapter_for=adapter_for)


def from_another_client(action: Callable[[], object]) -> None:
    def run() -> None:
        try:
            action()
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(run).result(timeout=30)


def another_due_account(system: System) -> ProviderAccount:
    account = operations.create_account(
        system.admin, provider="fixture", external_account_id="second-customer", api_key=""
    )
    ProviderAccount.objects.filter(pk=account.pk).update(revalidation_due_at=NOW)
    return account


def validation_attempts(binding: SiteBinding) -> int:
    return RequestAttempt.objects.filter(binding=binding, purpose="validation").count()


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

    outcome = revalidate(system, adapters(FakeAdapter()))

    assert outcome == RevalidationOutcome.DONE
    for binding in (system.binding, second):
        binding.refresh_from_db()
        assert binding.validated_credential_revision == 2
    assert due(system) is None
    validated = AuditEvent.objects.filter(action="connection_validated", revision__gte=1)
    assert set(validated.values_list("actor_id", flat=True)) == {system.admin.pk}


def test_a_rejected_new_key_stops_revalidation_of_the_other_plants() -> None:
    system = make_system(approved=True)
    second_plant(system)
    replaced(system)

    def rejected() -> None:
        raise ProviderError("provider_request_failed", retryable=False, status_code=401)

    outcome = revalidate(system, adapters(FakeAdapter(rejected)))

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

    outcome = revalidate(system, adapter)

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

    outcome = revalidate(system, adapters(FakeAdapter(unreachable)))

    assert outcome == RevalidationOutcome.WAITING
    retry = due(system)
    assert retry is not None
    assert (
        NOW + revalidation.RETRY_DELAY
        <= retry
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

    outcome = revalidate(system, adapter)

    assert outcome == RevalidationOutcome.WAITING
    assert len(validated) == 1
    next_due = NOW + LAST_REQUEST_START
    assert due(system) == next_due
    assert claim_due_revalidations(next_due) == [system.account.pk]
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

    outcome = revalidate(system, adapters(FakeAdapter()))

    assert outcome == RevalidationOutcome.WAITING
    assert due(system) == NOW + timedelta(hours=2)


def test_an_operator_who_lost_platform_access_ends_the_revalidation() -> None:
    system = make_system(approved=True)
    replaced(system)
    system.admin.is_superuser = False
    system.admin.save(update_fields=("is_superuser",))

    outcome = revalidate(system, adapters(FakeAdapter()))

    assert outcome == RevalidationOutcome.STOPPED
    assert due(system) is None
    assert not RequestAttempt.objects.filter(created_at=NOW).exists()


def test_an_account_without_a_replacement_is_left_alone() -> None:
    system = make_system(approved=True)
    ProviderAccount.objects.filter(pk=system.account.pk).update(revalidation_due_at=NOW)

    outcome = revalidate(system, adapters(FakeAdapter()))

    assert outcome == RevalidationOutcome.DONE
    assert RequestAttempt.objects.filter(created_at=NOW).count() == 0
    assert due(system) is None


def test_the_dispatcher_sends_each_due_account_with_its_lease_to_the_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = make_system(approved=True)
    replaced(system)
    broker = StubBroker()
    actor = REVALIDATION.make_actor(broker)
    monkeypatch.setattr("pyracast.providers.adapter_for", adapters(FakeAdapter()))

    assert dispatch_revalidations(actor) == 1

    message = Message.decode(broker.queues[actor.queue_name].get_nowait())
    assert message.args == (system.account.pk,)
    actor.fn(*message.args, **message.kwargs)
    system.binding.refresh_from_db()
    assert system.binding.validated_credential_revision == 2
    assert due(system) is None


def test_the_worker_revalidates_with_the_configured_provider(
    monkeypatch: pytest.MonkeyPatch, pyracast_logs: pytest.LogCaptureFixture
) -> None:
    system = make_system(approved=True)
    replaced(system)
    monkeypatch.setattr("pyracast.providers.adapter_for", adapters(FakeAdapter()))
    lease = due(system)
    assert lease is not None

    deliver_revalidation(system.account.pk, lease.isoformat())

    system.binding.refresh_from_db()
    assert system.binding.validated_credential_revision == 2
    assert f"account_id={system.account.pk} outcome=done" in pyracast_logs.text


def test_a_message_without_a_lease_runs_nothing(
    monkeypatch: pytest.MonkeyPatch, pyracast_logs: pytest.LogCaptureFixture
) -> None:
    system = make_system(approved=True)
    replaced(system)
    adapter_for = adapters(FakeAdapter())
    monkeypatch.setattr("pyracast.providers.adapter_for", adapter_for)

    deliver_revalidation(system.account.pk)

    adapter_for.assert_not_called()
    assert f"account_id={system.account.pk} outcome=lease_lost" in pyracast_logs.text


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

    outcome = revalidate(system, adapters(FakeAdapter()))

    assert outcome == RevalidationOutcome.STOPPED
    assert due(system) is None


def test_a_key_already_rejected_stops_revalidation_before_any_request() -> None:
    system = make_system(approved=True)
    replaced(system)
    ProviderAccount.objects.filter(pk=system.account.pk).update(authentication_failed_revision=2)
    attempts = RequestAttempt.objects.count()

    outcome = revalidate(system, adapters(FakeAdapter()))

    assert outcome == RevalidationOutcome.KEY_REJECTED
    assert RequestAttempt.objects.count() == attempts


def test_a_plant_changed_meanwhile_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    system = make_system(approved=True)
    replaced(system)

    def changed(*args: object, **kwargs: object) -> None:
        raise ValidationError("This provider binding is no longer current.")

    monkeypatch.setattr(revalidation, "validate_binding", changed)

    outcome = revalidate(system, adapters(FakeAdapter()))

    assert outcome == RevalidationOutcome.DONE


def test_a_replacement_during_revalidation_keeps_its_own_due_time() -> None:
    system = make_system(approved=True)
    replaced(system)

    def replaced_again() -> None:
        replace_key(system.admin, system.account.pk, provider_key("NEWER"))

    revalidate(system, adapters(FakeAdapter(replaced_again)))

    account = ProviderAccount.objects.get(pk=system.account.pk)
    assert account.credential_revision == 3
    assert account.revalidation_due_at is not None


def test_a_key_replaced_during_one_plants_validation_ends_the_run() -> None:
    system = make_system(approved=True)
    second = second_plant(system)
    replaced(system)
    attempts = {binding.pk: validation_attempts(binding) for binding in (system.binding, second)}
    seen: list[int] = []

    def adapter(binding: SiteBinding) -> ProviderAdapter:
        def validate() -> None:
            seen.append(binding.pk)
            if len(seen) == 1:
                from_another_client(
                    lambda: replace_key(system.admin, system.account.pk, provider_key("NEWER"))
                )

        return FakeAdapter(validate)

    outcome = revalidate(system, adapter)

    assert outcome == RevalidationOutcome.STOPPED
    assert len(seen) == 1
    later = next(b for b in (system.binding, second) if b.pk != seen[0])
    later.refresh_from_db()
    assert later.validated_credential_revision == 1
    assert validation_attempts(later) == attempts[later.pk]
    account = ProviderAccount.objects.get(pk=system.account.pk)
    assert account.credential_revision == 3
    assert account.revalidation_due_at is not None
    assert account.revalidation_due_at <= timezone.now()


def test_a_key_replaced_between_two_plants_ends_the_run() -> None:
    system = make_system(approved=True)
    second = second_plant(system)
    replaced(system)
    attempts = {binding.pk: validation_attempts(binding) for binding in (system.binding, second)}
    asked: list[int] = []

    def adapter(binding: SiteBinding) -> ProviderAdapter:
        asked.append(binding.pk)
        if len(asked) == 2:
            from_another_client(
                lambda: replace_key(system.admin, system.account.pk, provider_key("NEWER"))
            )
        return FakeAdapter()

    outcome = revalidate(system, adapter)

    assert outcome == RevalidationOutcome.STOPPED
    first, later = (SiteBinding.objects.get(pk=pk) for pk in asked)
    assert {first.pk, later.pk} == {system.binding.pk, second.pk}
    assert first.validated_credential_revision == 2
    assert later.validated_credential_revision == 1
    assert validation_attempts(later) == attempts[later.pk]


def test_the_worker_takes_the_lease_when_it_starts() -> None:
    system = make_system(approved=True)
    replaced(system)
    during: list[datetime | None] = []

    outcome = revalidate(system, adapters(FakeAdapter(lambda: during.append(due(system)))))

    assert outcome == RevalidationOutcome.DONE
    assert during == [NOW + revalidation.LEASE]


def test_a_message_whose_lease_the_dispatcher_took_again_runs_nothing() -> None:
    system = make_system(approved=True)
    replaced(system)
    first_dispatch = timezone.now()
    assert claim_due_revalidations(first_dispatch) == [system.account.pk]
    stale = due(system)
    assert claim_due_revalidations(first_dispatch + revalidation.LEASE) == [system.account.pk]
    adapter_for = adapters(FakeAdapter())

    assert revalidate(system, adapter_for, claim=stale) == RevalidationOutcome.LEASE_LOST
    adapter_for.assert_not_called()
    assert revalidate(system, adapter_for) == RevalidationOutcome.DONE
    assert validation_attempts(system.binding) == 2


def test_a_duplicate_message_after_a_back_off_runs_nothing() -> None:
    system = make_system(approved=True)
    replaced(system)
    claim = due(system)
    assert claim is not None

    def unreachable() -> None:
        raise ProviderError("provider_request_failed")

    assert revalidate(system, adapters(FakeAdapter(unreachable))) == RevalidationOutcome.WAITING
    backed_off = due(system)
    adapter_for = adapters(FakeAdapter())

    assert revalidate(system, adapter_for, claim=claim) == RevalidationOutcome.LEASE_LOST
    adapter_for.assert_not_called()
    assert due(system) == backed_off


def test_two_workers_given_the_same_lease_run_it_once() -> None:
    system = make_system(approved=True)
    replaced(system)
    claim = due(system)
    assert claim is not None
    barrier = Barrier(2)

    def worker() -> RevalidationOutcome:
        try:
            barrier.wait(timeout=10)
            return revalidate_account(
                system.account.pk, NOW, claim=claim, adapter_for=adapters(FakeAdapter())
            )
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = sorted(pool.map(lambda _: worker(), range(2)))

    assert outcomes == sorted([RevalidationOutcome.DONE, RevalidationOutcome.LEASE_LOST])
    assert validation_attempts(system.binding) == 2


def test_a_full_queue_publishes_nothing_and_claims_nothing() -> None:
    system = make_system(approved=True)
    replaced(system)
    broker = StubBroker()
    actor = REVALIDATION.make_actor(broker)
    for _ in range(QUEUE_DEPTH_LIMIT):
        actor.send(0)
    before = due(system)

    assert dispatch_revalidations(actor) == 0

    assert due(system) == before
    assert broker.queues[actor.queue_name].qsize() == QUEUE_DEPTH_LIMIT


def test_a_failed_publish_leaves_the_account_due_for_the_next_dispatch() -> None:
    system = make_system(approved=True)
    second = another_due_account(system)
    replaced(system)
    actor = MagicMock()
    actor.send_with_options.side_effect = [MagicMock(), ConnectionError("broker down")]

    with pytest.raises(ConnectionError):
        dispatch_revalidations(actor)

    sent = actor.send_with_options.call_args_list[0].kwargs["args"][0]
    unsent = ({system.account.pk, second.pk} - {sent}).pop()
    assert claim_due_revalidations(timezone.now()) == [unsent]
