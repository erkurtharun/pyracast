from datetime import UTC, datetime, timedelta

import pytest

from pyracast.connections.models import SiteBinding
from pyracast.forecasts.budget import remaining_forecast_requests
from pyracast.forecasts.models import CollectionJob, CollectionSchedule, RequestAttempt
from pyracast.forecasts.quota import ForecastBudgetReservedError
from pyracast.forecasts.revalidation import RETRY_DELAY, RevalidationOutcome
from pyracast.forecasts.validation import validate_binding
from pyracast.platform_admin.provider_accounts import account_context
from tests.support.domain import NOW, FakeAdapter
from tests.support.revalidation import (
    Run,
    account,
    collecting_account,
    run_revalidations,
    slots_of,
    spend_until,
    used,
    utc,
)

pytestmark = pytest.mark.django_db(transaction=True)
D1, D2, D3, D4 = (NOW.date() + timedelta(days=offset) for offset in range(1, 5))
WAITING, DONE = RevalidationOutcome.WAITING, RevalidationOutcome.DONE


def revision(binding: SiteBinding) -> int:
    binding.refresh_from_db()
    return binding.validated_credential_revision or 0


def validation_requests(binding: SiteBinding, since: datetime) -> int:
    return RequestAttempt.objects.filter(
        binding=binding, purpose="validation", created_at__gte=since
    ).count()


def unsucceeded_forecasts(plants: list[SiteBinding], since: datetime) -> set[tuple[int, datetime]]:
    return {
        (plants.index(job.binding), job.scheduled_for)
        for job in CollectionJob.objects.filter(
            binding__in=plants, endpoint="forecasts", scheduled_for__gte=since
        )
        .exclude(status="succeeded")
        .select_related("binding")
    }


def test_a_waiting_plant_never_takes_the_request_a_current_plants_later_forecast_needs() -> None:
    system, (current, waiting) = collecting_account([["16:00"], ["15:00"]])
    validate_binding(system.admin, current.pk, FakeAdapter(), utc(D1, 10))
    spend_until(system, utc(D1, 11), 8)

    runs = run_revalidations(system, slots_of([D1], [12, 13]), utc(D1, 11), utc(D2, 0))

    assert runs
    assert {run.outcome for run in runs} == {WAITING}
    assert revision(waiting) == 1
    assert validation_requests(waiting, utc(D1, 0)) == 0
    assert CollectionJob.objects.get(
        binding=current, endpoint="forecasts", scheduled_for=utc(D1, 13)
    ).status == ("succeeded")


def test_plants_booking_the_quota_are_revalidated_as_slots_pass_without_losing_a_forecast() -> None:
    system, plants = collecting_account([["06:00", "16:00"]] * 5)

    runs = run_revalidations(system, slots_of([D1, D2, D3], [3, 13]), utc(D1, 0, 30), utc(D4, 0))

    assert runs == [
        Run(utc(D1, 0, 30), WAITING, utc(D1, 3) + RETRY_DELAY, 3),
        Run(utc(D1, 3) + RETRY_DELAY, WAITING, utc(D1, 13) + RETRY_DELAY, 3),
        Run(utc(D1, 13) + RETRY_DELAY, WAITING, utc(D2, 0), 3),
        Run(utc(D2, 0), WAITING, utc(D2, 3) + RETRY_DELAY, 4),
        Run(utc(D2, 3) + RETRY_DELAY, WAITING, utc(D2, 13) + RETRY_DELAY, 4),
        Run(utc(D2, 13) + RETRY_DELAY, WAITING, utc(D3, 0), 4),
        Run(utc(D3, 0), WAITING, utc(D3, 3) + RETRY_DELAY, 4),
        Run(utc(D3, 3) + RETRY_DELAY, WAITING, utc(D3, 13) + RETRY_DELAY, 4),
        Run(utc(D3, 13) + RETRY_DELAY, DONE, None, 5),
    ]
    assert [revision(plant) for plant in plants] == [2] * 5
    assert unsucceeded_forecasts(plants, utc(D1, 0)) == {(3, utc(D2, 13))}


def test_a_single_waiting_plant_whose_forecast_is_due_waits_for_the_next_provider_day() -> None:
    system, (waiting,) = collecting_account([["16:00"]])
    spend_until(system, utc(D1, 12), 9)

    with pytest.raises(ForecastBudgetReservedError):
        validate_binding(system.admin, waiting.pk, FakeAdapter(), utc(D1, 12))
    runs = run_revalidations(system, slots_of([D1, D2], [13]), utc(D1, 12), utc(D3, 0))

    assert runs == [
        Run(utc(D1, 12), WAITING, utc(D1, 13) + RETRY_DELAY, 0),
        Run(utc(D1, 13) + RETRY_DELAY, WAITING, utc(D2, 0), 0),
        Run(utc(D2, 0), DONE, None, 1),
    ]
    assert used(system, utc(D1, 12)) == 9


def test_a_used_up_quota_waits_for_the_next_provider_day_even_before_a_slot() -> None:
    system, (waiting,) = collecting_account([["16:00"]])
    spend_until(system, utc(D1, 12), 10)

    runs = run_revalidations(system, [], utc(D1, 12), utc(D1, 13))

    assert runs == [Run(utc(D1, 12), WAITING, utc(D2, 0), 0)]
    assert revision(waiting) == 1


def test_the_previous_days_due_slot_is_counted_only_within_the_starts_claim_admits() -> None:
    system, (waiting,) = collecting_account([["16:00"]])
    start = utc(D2, 0)

    assert remaining_forecast_requests(account(system), start) == 1
    runs = run_revalidations(system, slots_of([D2], [13]), start, utc(D3, 0))

    assert runs == [Run(start, DONE, None, 1)]
    forecasts = RequestAttempt.objects.filter(binding=waiting, purpose="forecast", utc_day=D2)
    assert list(forecasts.values_list("job__scheduled_for", flat=True)) == [utc(D1, 13)]
    refused = CollectionJob.objects.get(
        binding=waiting, endpoint="forecasts", scheduled_for=utc(D2, 13)
    )
    assert (refused.status, refused.last_error) == ("stale", "quota_exhausted")


def test_a_plant_whose_need_completes_the_quota_waits_without_costing_a_current_forecast() -> None:
    system, plants = collecting_account([["16:00"]] * 10)
    *current, waiting = plants
    SiteBinding.objects.filter(pk__in=[binding.pk for binding in current]).update(
        validated_credential_revision=2
    )

    runs = run_revalidations(system, slots_of([D1, D2], [13]), utc(D1, 0), utc(D3, 0))

    assert runs == [
        Run(utc(D1, 0), WAITING, utc(D1, 13) + RETRY_DELAY, 9),
        Run(utc(D1, 13) + RETRY_DELAY, WAITING, utc(D2, 0), 9),
        Run(utc(D2, 0), WAITING, utc(D2, 13) + RETRY_DELAY, 9),
        Run(utc(D2, 13) + RETRY_DELAY, WAITING, utc(D3, 0), 9),
    ]
    assert revision(waiting) == 1
    assert validation_requests(waiting, utc(D1, 0)) == 0
    assert unsucceeded_forecasts(current, utc(D1, 0)) == set()
    assert {used(system, utc(day, 23)) for day in (D1, D2)} == {9}
    assert account_context(account(system), datetime.now(UTC))["waiting_plants"] == 1


def test_a_refused_revalidation_retries_after_the_next_slot_of_a_plant_that_collects() -> None:
    system, (current, waiting) = collecting_account([["16:00"], ["15:00"]])
    CollectionSchedule.objects.filter(region_id=waiting.region_id).update(enabled=False)
    validate_binding(system.admin, current.pk, FakeAdapter(), utc(D1, 10))
    spend_until(system, utc(D1, 11), 9)

    runs = run_revalidations(system, [], utc(D1, 11), utc(D1, 12))

    assert runs == [Run(utc(D1, 11), WAITING, utc(D1, 13) + RETRY_DELAY, 1)]
