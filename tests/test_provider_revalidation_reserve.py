from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from threading import Event

import pytest
from django.db import connections

from pyracast.connections.contracts import ProviderError
from pyracast.connections.models import ProviderAccount, SiteBinding
from pyracast.forecasts.budget import remaining_forecast_requests
from pyracast.forecasts.jobs.claim import claim
from pyracast.forecasts.models import (
    CollectionJob,
    CollectionSchedule,
    RequestAttempt,
    RequestPurpose,
)
from pyracast.forecasts.quota import ForecastBudgetReservedError, reserve_request
from pyracast.forecasts.revalidation import (
    RevalidationOutcome,
    revalidate_account,
    stale_bindings,
)
from pyracast.forecasts.scheduler import enqueue_due
from pyracast.forecasts.validation import validate_binding
from pyracast.plants.approval import approve_region
from pyracast.platform_admin.provider_accounts import account_context
from pyracast.regions.operations import configure_binding, submit_region
from tests.support.domain import NOW, FakeAdapter, System, make_system, provider_key
from tests.support.jobs import calculate, collect
from tests.support.providers import replace_key
from tests.support.revalidation import (
    assert_current_plants_keep_their_forecasts,
    collecting_account,
    dispatch_round,
    spend_until,
    used,
    utc,
)

pytestmark = pytest.mark.django_db(transaction=True)
NEXT_UTC_DAY = datetime.combine(NOW.date() + timedelta(days=1), datetime.min.time(), UTC)
DAY = NEXT_UTC_DAY.date()
LATER_DAY = DAY + timedelta(days=1)


def collecting_system() -> System:
    system = make_system(approved=True)
    CollectionSchedule.objects.create(
        region=system.region, times=["16:00"], effective_from=NOW - timedelta(days=1)
    )
    return system


def another_plant(system: System, name: str, *, approved: bool) -> SiteBinding:
    region = submit_region(
        system.member,
        system.tenant.pk,
        name=name,
        latitude=41,
        longitude=30,
        timezone="Europe/Istanbul",
    )
    binding = configure_binding(
        system.admin,
        region.pk,
        provider_account_id=system.account.pk,
        site_id=name,
        capacity_kw=1,
        capacity_semantics_verified=True,
    )
    if approved:
        validate_binding(system.admin, binding.pk, FakeAdapter(), NOW - timedelta(hours=1))
        approve_region(system.admin, region.pk, binding.pk, NOW)
    return binding


def leave_requests(system: System, left: int) -> None:
    used = RequestAttempt.objects.filter(provider_account=system.account, utc_day=NOW.date())
    ProviderAccount.objects.filter(pk=system.account.pk).update(daily_quota=used.count() + left)


def account(system: System) -> ProviderAccount:
    return ProviderAccount.objects.get(pk=system.account.pk)


def test_a_replaced_key_keeps_the_forecasts_its_plants_still_need_today() -> None:
    system = collecting_system()
    before = remaining_forecast_requests(account(system), NOW)
    replace_key(system.admin, system.account.pk, provider_key("NEW"))

    assert before == 1
    assert remaining_forecast_requests(account(system), NOW) == 2
    assert account_context(account(system), NOW)["budget"].reserved == 2


def test_revalidating_a_collecting_plant_never_takes_a_request_a_forecast_needs() -> None:
    system = collecting_system()
    replace_key(system.admin, system.account.pk, provider_key("NEW"))
    leave_requests(system, 1)
    attempts = RequestAttempt.objects.count()

    with pytest.raises(ForecastBudgetReservedError):
        validate_binding(system.admin, system.binding.pk, FakeAdapter(), NOW)

    assert RequestAttempt.objects.count() == attempts


def test_the_second_plant_keeps_its_forecast_after_the_first_was_revalidated() -> None:
    system = collecting_system()
    second = another_plant(system, "second-site", approved=True)
    replace_key(system.admin, system.account.pk, provider_key("NEW"))
    validate_binding(system.admin, system.binding.pk, FakeAdapter(), NOW)
    CollectionJob.objects.filter(binding=system.binding).update(status="succeeded")
    leave_requests(system, 1)
    attempts = RequestAttempt.objects.count()

    with pytest.raises(ForecastBudgetReservedError):
        validate_binding(system.admin, second.pk, FakeAdapter(), NOW)

    assert RequestAttempt.objects.count() == attempts
    second.refresh_from_db()
    assert second.validated_credential_revision == 1


def test_revalidation_keeps_the_forecast_reserve_and_continues_the_next_utc_day() -> None:
    system = collecting_system()
    replace_key(system.admin, system.account.pk, provider_key("NEW"))
    leave_requests(system, 1)
    attempts = RequestAttempt.objects.count()
    claim = account(system).revalidation_due_at
    assert claim is not None

    outcome = revalidate_account(
        system.account.pk, NOW, claim=claim, adapter_for=lambda binding: FakeAdapter()
    )

    assert outcome == RevalidationOutcome.WAITING
    assert account(system).revalidation_due_at == NEXT_UTC_DAY
    assert RequestAttempt.objects.count() == attempts
    system.binding.refresh_from_db()
    assert system.binding.validated_credential_revision == 1


def test_revalidating_a_collecting_plant_spends_what_the_forecasts_leave() -> None:
    system = collecting_system()
    replace_key(system.admin, system.account.pk, provider_key("NEW"))
    leave_requests(system, 2)

    validate_binding(system.admin, system.binding.pk, FakeAdapter(), NOW)

    system.binding.refresh_from_db()
    assert system.binding.validated_credential_revision == 2


def rejected() -> None:
    raise ProviderError("provider_request_failed", status_code=401, retryable=False)


def waiting_plant_with_slots_to_come() -> tuple[System, SiteBinding, datetime]:
    system, (current, _waiting) = collecting_account([["16:00"], ["15:00"]])
    validate_binding(system.admin, current.pk, FakeAdapter(), utc(DAY, 10))
    return system, current, utc(DAY, 11)


def waiting_plant_whose_slot_passed() -> tuple[System, SiteBinding, datetime]:
    system, (current, _waiting) = collecting_account([["16:00"], ["15:00"]])
    validate_binding(system.admin, current.pk, FakeAdapter(), utc(DAY, 10))
    return system, current, utc(DAY, 12, 30)


def waiting_plant_with_a_job_ended_as_not_current() -> tuple[System, SiteBinding, datetime]:
    system, (current, waiting) = collecting_account([["16:00"], ["15:00"]], replaced=False)
    enqueue_due(utc(DAY, 12))
    job = CollectionJob.objects.get(
        binding=waiting, endpoint="forecasts", scheduled_for=utc(DAY, 12)
    )
    replace_key(system.admin, system.account.pk, provider_key("NEW"))
    validate_binding(system.admin, current.pk, FakeAdapter(), utc(DAY, 12, 5))
    assert not collect(job.pk, FakeAdapter(), calculate, utc(DAY, 12, 10))
    job.refresh_from_db()
    assert (job.status, job.last_error) == ("stale", "binding_not_current")
    return system, current, utc(DAY, 12, 30)


def waiting_plant_with_a_job_its_validation_arms_again() -> tuple[System, SiteBinding, datetime]:
    system, (current, waiting) = collecting_account([["16:00"], ["15:00"]], replaced=False)
    enqueue_due(utc(DAY, 12))
    job = CollectionJob.objects.get(
        binding=waiting, endpoint="forecasts", scheduled_for=utc(DAY, 12)
    )
    assert not collect(job.pk, FakeAdapter(rejected), calculate, utc(DAY, 12))
    job.refresh_from_db()
    assert (job.status, job.last_error) == ("failed", "provider_authentication_failed")
    replace_key(system.admin, system.account.pk, provider_key("NEW"))
    validate_binding(system.admin, current.pk, FakeAdapter(), utc(DAY, 12, 5))
    return system, current, utc(DAY, 12, 30)


@pytest.mark.parametrize(
    "arrange",
    [
        waiting_plant_with_slots_to_come,
        waiting_plant_whose_slot_passed,
        waiting_plant_with_a_job_ended_as_not_current,
        waiting_plant_with_a_job_its_validation_arms_again,
    ],
)
def test_a_history_request_leaves_the_forecast_a_waiting_plant_needs_once_revalidated(
    arrange: Callable[[], tuple[System, SiteBinding, datetime]],
) -> None:
    system, current, moment = arrange()
    spend_until(system, moment, 8)
    history = history_job(current, moment)

    assert remaining_forecast_requests(account(system), moment) == 3
    with pytest.raises(ForecastBudgetReservedError):
        reserve_request(current, moment, purpose=RequestPurpose.ESTIMATED_ACTUALS, job=history)

    assert used(system, moment) == 8


def test_a_history_request_leaves_a_waiting_plant_the_request_of_its_revalidation() -> None:
    system, current, moment = waiting_plant_with_slots_to_come()
    spend_until(system, moment, 7)

    with pytest.raises(ForecastBudgetReservedError):
        reserve_request(
            current,
            moment,
            purpose=RequestPurpose.ESTIMATED_ACTUALS,
            job=history_job(current, moment),
        )
    assert used(system, moment) == 7

    claim = account(system).revalidation_due_at
    assert claim is not None
    outcome = revalidate_account(
        system.account.pk, moment, claim=claim, adapter_for=lambda binding: FakeAdapter()
    )

    assert outcome == RevalidationOutcome.DONE
    assert used(system, moment) == 8
    assert not stale_bindings(account(system)).exists()
    assert_current_plants_keep_their_forecasts(system, moment)


def history_job(binding: SiteBinding, moment: datetime) -> CollectionJob:
    return CollectionJob.objects.create(
        binding=binding,
        endpoint="estimated_actuals",
        scheduled_for=moment,
        target_date=moment.date(),
        available_at=moment,
        retry_until=moment + timedelta(hours=1),
    )


def test_the_due_slot_of_a_current_plant_is_kept_before_its_job_is_created() -> None:
    system, (current, waiting) = collecting_account([["16:00"], ["15:00"]])
    CollectionSchedule.objects.filter(region_id=waiting.region_id).update(enabled=False)
    validate_binding(system.admin, current.pk, FakeAdapter(), utc(DAY, 10))
    moment = utc(DAY, 13, 5)
    spend_until(system, moment, 9)

    assert not CollectionJob.objects.filter(binding=current, scheduled_for=utc(DAY, 13)).exists()
    assert remaining_forecast_requests(account(system), moment) == 2
    with pytest.raises(ForecastBudgetReservedError):
        reserve_request(
            current,
            moment,
            purpose=RequestPurpose.ESTIMATED_ACTUALS,
            job=history_job(current, moment),
        )
    with pytest.raises(ForecastBudgetReservedError):
        validate_binding(system.admin, waiting.pk, FakeAdapter(), moment)

    assert used(system, moment) == 9


def test_the_first_validation_of_a_waiting_plant_is_not_held_back_by_the_reserve() -> None:
    system = collecting_system()
    waiting = another_plant(system, "waiting-site", approved=False)
    leave_requests(system, 1)
    assert remaining_forecast_requests(account(system), NOW) == 1

    validate_binding(system.admin, waiting.pk, FakeAdapter(), NOW)

    waiting.refresh_from_db()
    assert waiting.validated_credential_revision == 1


def revision(binding: SiteBinding) -> int:
    binding.refresh_from_db()
    return binding.validated_credential_revision or 0


@contextmanager
def validation_in_flight(system: System, binding: SiteBinding, moment: datetime) -> Iterator[None]:
    """Keeps another client's validation of the binding waiting for the provider's answer."""
    asked, answered = Event(), Event()

    def await_answer() -> None:
        asked.set()
        assert answered.wait(timeout=30)

    def validate() -> None:
        try:
            validate_binding(system.admin, binding.pk, FakeAdapter(await_answer), moment)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as pool:
        validation = pool.submit(validate)
        try:
            assert asked.wait(timeout=30)
            yield
        finally:
            answered.set()
        validation.result(timeout=30)


@pytest.mark.parametrize(
    ("clocks", "current", "spent"),
    [
        # Quota 10 at 11:00 UTC with nothing used. The current plant needs 4 (13:00 to 16:00
        # UTC), each waiting plant 3 (12:00 to 12:20 and 12:30 to 12:50 UTC). The first is
        # admitted at 0 + 1 + 4 + 3 = 8. With it in flight the second is refused at
        # 1 + 1 + (4 + 3) + 3 = 12 > 10; counted without it, 1 + 1 + 4 + 3 = 9 would admit it,
        # and both answers would leave 2 used + 10 needed = 12 > 10.
        pytest.param(
            [
                ["16:00", "17:00", "18:00", "19:00"],
                ["15:00", "15:10", "15:20"],
                ["15:30", "15:40", "15:50"],
            ],
            1,
            0,
            id="quota-10",
        ),
        # One clock each (12:00 and 13:00 UTC) with 7 used: the first at 7 + 1 + 0 + 1 = 9, the
        # second at 8 + 1 + 1 + 1 = 11 > 10 instead of 8 + 1 + 0 + 1 = 10, which would leave
        # 9 used + 2 needed = 11 > 10.
        pytest.param([["15:00"], ["16:00"]], 0, 7, id="one-clock-at-7"),
    ],
)
def test_a_waiting_plant_whose_validation_is_in_flight_keeps_its_forecasts_from_another(
    clocks: list[list[str]], current: int, spent: int
) -> None:
    system, plants = collecting_account(clocks)
    if current:
        validate_binding(system.admin, plants[0].pk, FakeAdapter(), utc(NOW.date(), 23))
    first, second = plants[current:]
    moment = utc(DAY, 11)
    spend_until(system, moment, spent)

    with validation_in_flight(system, first, moment), pytest.raises(ForecastBudgetReservedError):
        validate_binding(system.admin, second.pk, FakeAdapter(), moment)

    assert used(system, moment) == spent + 1
    assert [revision(first), revision(second)] == [2, 1]
    assert_current_plants_keep_their_forecasts(system, moment)


def retrying_older_job(waiting: SiteBinding) -> CollectionJob:
    """The state a server error with a long Retry-After leaves on the waiting plant's job of
    DAY 13:00 UTC: sent that day and retried late on the next."""
    job = CollectionJob.objects.create(
        binding=waiting,
        endpoint="forecasts",
        scheduled_for=utc(DAY, 13),
        target_date=DAY + timedelta(days=1),
        status="retry",
        attempts=1,
        last_error="provider_server_error",
        available_at=utc(LATER_DAY, 23),
    )
    RequestAttempt.objects.create(
        provider_account=waiting.provider_account,
        binding=waiting,
        job=job,
        utc_day=DAY,
        created_at=utc(DAY, 13),
        purpose="forecast",
        outcome="failed",
        http_status=503,
    )
    return job


def test_an_older_retrying_job_holds_back_neither_a_history_request_nor_the_revalidation() -> None:
    system, (current, waiting) = collecting_account([["06:00"], ["16:00"]], replaced=False)
    ProviderAccount.objects.filter(pk=system.account.pk).update(daily_quota=4)
    retrying_older_job(waiting)
    replace_key(system.admin, system.account.pk, provider_key("NEW"))
    validate_binding(system.admin, current.pk, FakeAdapter(), utc(DAY, 22))
    dispatch_round(utc(LATER_DAY, 3))
    moment = utc(LATER_DAY, 14)
    assert used(system, moment) == 1

    # The waiting plant needs only its due slot of 13:00 UTC: 1 + 1 + 1 + 0 + 1 = 4.
    reserve_request(
        current, moment, purpose=RequestPurpose.ESTIMATED_ACTUALS, job=history_job(current, moment)
    )
    # And then 2 + 1 + 0 + 1 = 4.
    validate_binding(system.admin, waiting.pk, FakeAdapter(), moment)

    assert used(system, moment) == 3
    assert revision(waiting) == 2
    assert_current_plants_keep_their_forecasts(system, moment)


def test_claim_ends_a_waiting_plants_job_older_than_the_due_slot_without_a_request() -> None:
    system, (_current, waiting) = collecting_account([["06:00"], ["16:00"]], replaced=False)
    older = retrying_older_job(waiting)
    replace_key(system.admin, system.account.pk, provider_key("NEW"))
    attempts = RequestAttempt.objects.count()

    assert claim(older.pk, utc(LATER_DAY, 23)) is None

    older.refresh_from_db()
    assert (older.status, older.last_error) == ("stale", "forecast_superseded")
    assert RequestAttempt.objects.count() == attempts
