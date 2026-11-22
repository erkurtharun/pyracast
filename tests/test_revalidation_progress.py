from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from django.core.exceptions import ValidationError

from pyracast import trash
from pyracast.background import clock as monotonic_clock
from pyracast.connections.contracts import ProviderAdapter, ProviderError
from pyracast.connections.models import ProviderAccount, SiteBinding
from pyracast.forecasts import budget
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
    AFTER_THE_SLOT,
    RETRY_DELAY,
    RevalidationOutcome,
    revalidate_account,
)
from pyracast.forecasts.schedule import configure_schedule
from pyracast.forecasts.scheduler import enqueue_due
from pyracast.forecasts.validation import validate_binding
from pyracast.platform_admin.provider_accounts import account_context
from pyracast.trash import restore_region, trash_region
from tests.support.domain import NOW, FakeAdapter, System, provider_key
from tests.support.jobs import calculate, collect
from tests.support.providers import replace_key
from tests.support.revalidation import (
    ROUND_OFFSETS,
    Run,
    account,
    assert_current_plants_keep_their_forecasts,
    collecting_account,
    dispatch_round,
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
        Run(utc(D1, 0, 30), WAITING, utc(D1, 3) + AFTER_THE_SLOT, 3),
        Run(utc(D1, 3) + AFTER_THE_SLOT, WAITING, utc(D1, 13) + AFTER_THE_SLOT, 3),
        Run(utc(D1, 13) + AFTER_THE_SLOT, WAITING, utc(D2, 0), 3),
        Run(utc(D2, 0), WAITING, utc(D2, 3) + AFTER_THE_SLOT, 4),
        Run(utc(D2, 3) + AFTER_THE_SLOT, WAITING, utc(D2, 13) + AFTER_THE_SLOT, 4),
        Run(utc(D2, 13) + AFTER_THE_SLOT, WAITING, utc(D3, 0), 4),
        Run(utc(D3, 0), WAITING, utc(D3, 3) + AFTER_THE_SLOT, 4),
        Run(utc(D3, 3) + AFTER_THE_SLOT, WAITING, utc(D3, 13) + AFTER_THE_SLOT, 4),
        Run(utc(D3, 13) + AFTER_THE_SLOT, DONE, None, 5),
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
        Run(utc(D1, 12), WAITING, utc(D1, 13) + AFTER_THE_SLOT, 0),
        Run(utc(D1, 13) + AFTER_THE_SLOT, WAITING, utc(D2, 0), 0),
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

    assert remaining_forecast_requests(account(system), start) == 2
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
        Run(utc(D1, 0), WAITING, utc(D1, 13) + AFTER_THE_SLOT, 9),
        Run(utc(D1, 13) + AFTER_THE_SLOT, WAITING, utc(D2, 0), 9),
        Run(utc(D2, 0), WAITING, utc(D2, 13) + AFTER_THE_SLOT, 9),
        Run(utc(D2, 13) + AFTER_THE_SLOT, WAITING, utc(D3, 0), 9),
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

    assert runs == [Run(utc(D1, 11), WAITING, utc(D1, 13) + AFTER_THE_SLOT, 1)]


def rejected() -> None:
    raise ProviderError("provider_request_failed", status_code=401, retryable=False)


def refused_for_the_key(binding: SiteBinding, slot: datetime) -> CollectionJob:
    enqueue_due(slot)
    job = CollectionJob.objects.get(binding=binding, endpoint="forecasts", scheduled_for=slot)
    assert not collect(job.pk, FakeAdapter(rejected), calculate, slot)
    job.refresh_from_db()
    assert (job.status, job.last_error) == ("failed", "provider_authentication_failed")
    return job


def assert_the_due_slot_was_collected_once_and_the_older_job_stays_ended(
    binding: SiteBinding, refused: CollectionJob, due: datetime
) -> None:
    refused.refresh_from_db()
    assert (refused.status, refused.last_error) == ("failed", "provider_authentication_failed")
    collected = CollectionJob.objects.get(binding=binding, endpoint="forecasts", scheduled_for=due)
    assert collected.status == "succeeded"
    assert RequestAttempt.objects.filter(job=collected).count() == 1
    assert not RequestAttempt.objects.filter(job=refused, created_at__gte=due).exists()


def test_a_plant_refused_for_the_old_key_before_a_passed_slot_is_revalidated_on_quota_two() -> None:
    system, (plant,) = collecting_account([["16:00"]], replaced=False)
    ProviderAccount.objects.filter(pk=system.account.pk).update(daily_quota=2)
    refused = refused_for_the_key(plant, utc(D1, 13))
    spend_until(system, utc(D2, 12), 1)
    replace_key(system.admin, system.account.pk, provider_key("NEW"))

    runs = run_revalidations(system, slots_of([D2, D3], [13]), utc(D2, 12), utc(D4, 0))

    # D2 12:00: 1 + 1 + 2 > 2, the refused job is the due slot's and comes back; after D2 13:00
    # 1 + 1 + 1 > 2 with only the due slot of D2 13:00; D3 00:00: 0 + 1 + 1 = 2.
    assert [(run.outcome, run.current) for run in runs] == [(WAITING, 0), (WAITING, 0), (DONE, 1)]
    assert runs[-1].at == utc(D3, 0)
    assert_the_due_slot_was_collected_once_and_the_older_job_stays_ended(
        plant, refused, utc(D2, 13)
    )


def test_nine_clocks_on_quota_ten_revalidate_a_plant_refused_for_the_old_key() -> None:
    system, plants = collecting_account([["06:00", "16:00"]] * 4 + [["16:00"]], replaced=False)
    *current, plant = plants
    refused = refused_for_the_key(plant, utc(D1, 13))
    replace_key(system.admin, system.account.pk, provider_key("NEW"))
    for binding in current:
        validate_binding(system.admin, binding.pk, FakeAdapter(), utc(D1, 22))

    runs = run_revalidations(system, slots_of([D2, D3], [3, 13]), utc(D2, 13, 15), utc(D4, 0))

    # 8 used by the current plants, which need nothing more today; the plant needs its due slot
    # of D2 13:00 only: 8 + 1 + 0 + 1 = 10.
    assert runs == [Run(utc(D2, 13, 15), DONE, None, 5)]
    assert_the_due_slot_was_collected_once_and_the_older_job_stays_ended(
        plant, refused, utc(D2, 13)
    )


def assert_the_refused_job_stays_ended_at_validation_and_claim(
    system: System, plant: SiteBinding, refused: CollectionJob, moment: datetime, need: int
) -> None:
    assert remaining_forecast_requests(account(system), moment, validating=plant) == need
    validate_binding(system.admin, plant.pk, FakeAdapter(), moment)
    assert_current_plants_keep_their_forecasts(system, moment)
    attempts = RequestAttempt.objects.count()
    refused.refresh_from_db()
    assert (refused.status, refused.last_error) == ("failed", "provider_authentication_failed")

    assert claim(refused.pk, moment) is None

    refused.refresh_from_db()
    assert (refused.status, refused.last_error) == ("failed", "provider_authentication_failed")
    assert RequestAttempt.objects.count() == attempts


def collect_the_slot(slot: datetime) -> None:
    for offset in ROUND_OFFSETS:
        dispatch_round(slot + offset)


def trash_clock_at(monkeypatch: pytest.MonkeyPatch, moment: datetime) -> None:
    monkeypatch.setattr(trash, "timezone", SimpleNamespace(now=lambda: moment))


@pytest.mark.parametrize("quota", [2, 10])
def test_a_job_refused_for_the_key_before_the_trash_stays_ended_after_the_restore(
    monkeypatch: pytest.MonkeyPatch, quota: int
) -> None:
    system, (plant,) = collecting_account([["16:00"]], replaced=False)
    ProviderAccount.objects.filter(pk=system.account.pk).update(daily_quota=quota)
    refused = refused_for_the_key(plant, utc(D1, 13))
    trash_clock_at(monkeypatch, utc(D1, 14))
    trash_region(system.member, system.tenant.pk, plant.region_id)
    replace_key(system.admin, system.account.pk, provider_key("NEW"))
    trash_clock_at(monkeypatch, utc(D2, 11))
    restore_region(system.member, system.tenant.pk, plant.region_id)

    # D2 11:00, nothing used: the restored schedule sends only the slot of D2 13:00, so the
    # validation fits 0 + 1 + 1 = 2 even on quota 2; the job of D1 13:00 is from before the restore.
    assert_the_refused_job_stays_ended_at_validation_and_claim(
        system, plant, refused, utc(D2, 11), need=1
    )
    collect_the_slot(utc(D2, 13))
    assert_the_due_slot_was_collected_once_and_the_older_job_stays_ended(
        plant, refused, utc(D2, 13)
    )


def test_a_job_refused_for_the_key_at_a_clock_taken_off_the_schedule_stays_ended() -> None:
    system, (plant,) = collecting_account([["16:00"]], replaced=False)
    refused = refused_for_the_key(plant, utc(D1, 13))
    configure_schedule(
        system.member,
        system.tenant.pk,
        plant.region_id,
        enabled=True,
        times=["18:00"],
        now=utc(D1, 13, 30),
    )
    replace_key(system.admin, system.account.pk, provider_key("NEW"))

    # Claim ends a 16:00 job unsent, and the refused request took the plant's one start of D1,
    # so the new clock's slot of D1 15:00 UTC is refused by the day bound: nothing is sent.
    assert_the_refused_job_stays_ended_at_validation_and_claim(
        system, plant, refused, utc(D1, 14), need=0
    )


def test_a_job_refused_for_the_key_stays_ended_while_collection_is_off() -> None:
    system, (plant,) = collecting_account([["16:00"]], replaced=False)
    refused = refused_for_the_key(plant, utc(D1, 13))
    configure_schedule(
        system.member,
        system.tenant.pk,
        plant.region_id,
        enabled=False,
        times=["16:00"],
        now=utc(D1, 13, 30),
    )
    replace_key(system.admin, system.account.pk, provider_key("NEW"))

    assert_the_refused_job_stays_ended_at_validation_and_claim(
        system, plant, refused, utc(D1, 14), need=0
    )


def refused_together_for_the_key(
    system: System, slot: datetime
) -> tuple[SiteBinding, CollectionJob, SiteBinding, CollectionJob]:
    """The plant revalidated first is refused 401 at the slot; the other's job then ends
    unsent, as the account waits for a new key."""
    first, second = budget.approved_bindings(account(system)).order_by("validated_at", "pk")
    refused = refused_for_the_key(first, slot)
    blocked = CollectionJob.objects.get(binding=second, endpoint="forecasts", scheduled_for=slot)
    assert not collect(blocked.pk, FakeAdapter(), calculate, slot)
    blocked.refresh_from_db()
    assert (blocked.status, blocked.last_error, blocked.attempts) == (
        "failed",
        "provider_authentication_failed",
        0,
    )
    return first, refused, second, blocked


@pytest.mark.parametrize(("quota", "earlier"), [(4, 0), (10, 6)])
def test_a_job_armed_again_earlier_in_the_run_keeps_its_room_from_the_next_plant(
    quota: int, earlier: int
) -> None:
    system, _plants = collecting_account([["16:00"], ["16:00"]], replaced=False)
    ProviderAccount.objects.filter(pk=system.account.pk).update(daily_quota=quota)
    spend_until(system, utc(D1, 12), earlier)
    first, refused, second, blocked = refused_together_for_the_key(system, utc(D1, 13))
    replace_key(system.admin, system.account.pk, provider_key("NEW"))
    lease = account(system).revalidation_due_at
    assert lease is not None
    moment = utc(D1, 13, 30)

    outcome = revalidate_account(
        system.account.pk, moment, claim=lease, adapter_for=lambda binding: FakeAdapter()
    )

    # The first plant fits earlier + 1 used + 1 + 1. Its job, reopened but refused once today, is
    # still sent: earlier + 2 used + 1 + 1 + 1 > quota for the second plant.
    assert outcome == WAITING
    assert [revision(first), revision(second)] == [2, 1]
    refused.refresh_from_db()
    assert refused.status == "pending"
    assert remaining_forecast_requests(account(system), moment, validating=second) == 2
    collect_the_slot(moment)
    refused.refresh_from_db()
    blocked.refresh_from_db()
    assert refused.status == "succeeded"
    assert (blocked.status, blocked.last_error) == ("failed", "provider_authentication_failed")
    assert used(system, moment) == earlier + 3
    assert remaining_forecast_requests(account(system), moment, validating=second) == 1
    assert not CollectionJob.objects.filter(
        binding__in=[first, second], last_error="quota_exhausted"
    ).exists()


def lower_the_quota_to_two(system: System) -> Callable[[], None]:
    def lower() -> None:
        ProviderAccount.objects.filter(pk=system.account.pk).update(daily_quota=2)

    return lower


def test_a_validation_arms_again_only_at_a_clock_the_locked_account_quota_keeps() -> None:
    system, (plant,) = collecting_account([["06:00", "16:00"]], replaced=False)
    ProviderAccount.objects.filter(pk=system.account.pk).update(daily_quota=3)
    refused = refused_for_the_key(plant, utc(D1, 13))
    replace_key(system.admin, system.account.pk, provider_key("NEW"))
    moment = utc(D1, 13, 30)

    validate_binding(system.admin, plant.pk, FakeAdapter(lower_the_quota_to_two(system)), moment)

    # Quota 2 keeps the clock of 06:00 only, and claim ends a job of 16:00 unsent.
    assert revision(plant) == 2
    refused.refresh_from_db()
    assert (refused.status, refused.last_error) == ("failed", "provider_authentication_failed")
    attempts = RequestAttempt.objects.count()
    assert claim(refused.pk, moment) is None
    refused.refresh_from_db()
    assert (refused.status, refused.last_error) == ("failed", "provider_authentication_failed")
    assert RequestAttempt.objects.count() == attempts


def left_ended_by_a_quota_lowered_during_the_validation(
    system: System, plant: SiteBinding
) -> CollectionJob:
    """Clocks of 06:00 and 16:00 on quota 3: the job of D1 13:00 UTC is refused for the key, the
    validation of 13:30 commits under quota 2 and leaves it ended, and the quota is 3 again."""
    ProviderAccount.objects.filter(pk=system.account.pk).update(daily_quota=3)
    refused = refused_for_the_key(plant, utc(D1, 13))
    replace_key(system.admin, system.account.pk, provider_key("NEW"))
    validate_binding(
        system.admin, plant.pk, FakeAdapter(lower_the_quota_to_two(system)), utc(D1, 13, 30)
    )
    ProviderAccount.objects.filter(pk=system.account.pk).update(daily_quota=3)
    refused.refresh_from_db()
    assert (refused.status, refused.last_error) == ("failed", "provider_authentication_failed")
    assert (revision(plant), used(system, utc(D1, 13, 30))) == (2, 2)
    return refused


def test_a_job_no_validation_arms_again_holds_back_no_history_request() -> None:
    system, (plant,) = collecting_account([["06:00", "16:00"]], replaced=False)
    refused = left_ended_by_a_quota_lowered_during_the_validation(system, plant)
    moment = utc(D1, 13, 45)

    # The plant is validated with the current key, so nothing reopens the job: 2 + 1 + 0 = 3.
    assert remaining_forecast_requests(account(system), moment) == 0
    reserve_request(
        plant, moment, purpose=RequestPurpose.ESTIMATED_ACTUALS, keep_forecast_reserve=True
    )
    assert used(system, moment) == 3
    assert claim(refused.pk, moment) is None
    refused.refresh_from_db()
    assert (refused.status, refused.last_error) == ("failed", "provider_authentication_failed")


def test_validating_a_current_plant_again_leaves_its_job_refused_for_the_key_ended() -> None:
    system, (plant,) = collecting_account([["06:00", "16:00"]], replaced=False)
    refused = left_ended_by_a_quota_lowered_during_the_validation(system, plant)
    moment = utc(D1, 13, 45)

    validate_binding(system.admin, plant.pk, FakeAdapter(), moment)

    # Admitted at 2 + 1 + 0 = 3, as the count saw the job; reopening it would need a fourth.
    refused.refresh_from_db()
    assert (refused.status, refused.last_error) == ("failed", "provider_authentication_failed")
    assert used(system, moment) == 3
    assert_current_plants_keep_their_forecasts(system, moment)


def recording(asked: list[int]) -> Callable[[SiteBinding], ProviderAdapter]:
    def adapter_for(binding: SiteBinding) -> ProviderAdapter:
        asked.append(binding.pk)
        return FakeAdapter()

    return adapter_for


def test_a_plant_refused_for_the_reserve_leaves_the_later_waiting_plants_their_turn() -> None:
    system, plants = collecting_account(
        [["16:00"]] * 8 + [["06:00", "16:00"], ["16:00"], ["16:00"]]
    )
    *current, refused, first, second = plants
    SiteBinding.objects.filter(pk__in=[binding.pk for binding in current]).update(
        validated_credential_revision=2
    )
    CollectionSchedule.objects.filter(region_id__in=[first.region_id, second.region_id]).update(
        enabled=False
    )
    claim = account(system).revalidation_due_at
    assert claim is not None
    asked: list[int] = []

    # 8 needed by the current plants: the two-clock plant at 0 + 1 + 8 + 2 = 11 > 10, then the
    # plants with collection off at 0 + 1 + 8 + 0 = 9 and 1 + 1 + 8 + 0 = 10.
    outcome = revalidate_account(
        system.account.pk, utc(D1, 0), claim=claim, adapter_for=recording(asked)
    )

    assert outcome == WAITING
    due = account(system).revalidation_due_at
    assert due is not None
    assert utc(D1, 3) < due <= utc(D1, 3) + RETRY_DELAY
    assert asked == [refused.pk, first.pk, second.pk]
    assert [revision(refused), revision(first), revision(second)] == [1, 2, 2]
    assert used(system, utc(D1, 0)) == 2
    assert_current_plants_keep_their_forecasts(system, utc(D1, 0))


def test_a_run_whose_every_plant_is_refused_for_the_reserve_waits_for_the_next_slot() -> None:
    system, plants = collecting_account([["16:00"]] * 11)
    *current, first, second = plants
    SiteBinding.objects.filter(pk__in=[binding.pk for binding in current]).update(
        validated_credential_revision=2
    )
    claim = account(system).revalidation_due_at
    assert claim is not None
    asked: list[int] = []

    # 9 needed by the current plants: each waiting plant at 0 + 1 + 9 + 1 = 11 > 10.
    outcome = revalidate_account(
        system.account.pk, utc(D1, 0), claim=claim, adapter_for=recording(asked)
    )

    assert outcome == WAITING
    due = account(system).revalidation_due_at
    assert due is not None
    assert utc(D1, 13) < due <= utc(D1, 13) + RETRY_DELAY
    assert asked == [first.pk, second.pk]
    assert [revision(first), revision(second)] == [1, 1]
    assert used(system, utc(D1, 0)) == 0


def test_plants_with_a_clock_late_in_the_utc_day_are_revalidated_right_after_it() -> None:
    system, plants = collecting_account([["06:00", "18:45"]] * 5, timezone="America/New_York")
    slots = [utc(day, hour, minute) for day in (D1, D2, D3) for hour, minute in ((11, 0), (23, 45))]

    runs = run_revalidations(system, slots, utc(D1, 0, 30), utc(D4, 0))

    for run in runs:
        assert run.due is None or run.at < run.due <= utc(run.at.date() + timedelta(days=1), 0)
    after = AFTER_THE_SLOT
    assert runs == [
        Run(utc(D1, 0, 30), WAITING, utc(D1, 11) + after, 3),
        Run(utc(D1, 11) + after, WAITING, utc(D1, 23, 45) + after, 3),
        Run(utc(D1, 23, 45) + after, WAITING, utc(D2, 0), 3),
        Run(utc(D2, 0), WAITING, utc(D2, 11) + after, 4),
        Run(utc(D2, 11) + after, WAITING, utc(D2, 23, 45) + after, 4),
        Run(utc(D2, 23, 45) + after, WAITING, utc(D3, 0), 4),
        Run(utc(D3, 0), WAITING, utc(D3, 11) + after, 4),
        Run(utc(D3, 11) + after, WAITING, utc(D3, 23, 45) + after, 4),
        Run(utc(D3, 23, 45) + after, DONE, None, 5),
    ]
    assert [revision(plant) for plant in plants] == [2] * 5


class SlowlyFailingPreparation(FakeAdapter):
    """Takes six seconds to prepare and fails before any request."""

    def __init__(self, ticks: list[float]) -> None:
        super().__init__()
        self.ticks = ticks

    def prepare(self, binding: SiteBinding) -> None:
        self.ticks[0] += 6
        raise ValidationError("Configuration changed during validation.")


def test_a_run_refused_for_the_reserve_is_due_after_the_slot_following_its_first_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plants = collecting_account([["16:00"]] * 11)
    *current, refused, slow = plants
    SiteBinding.objects.filter(pk__in=[binding.pk for binding in current]).update(
        validated_credential_revision=2
    )
    lease = account(system).revalidation_due_at
    assert lease is not None
    ticks = [0.0]
    monkeypatch.setattr(monotonic_clock, "time", SimpleNamespace(monotonic=lambda: ticks[0]))

    # The first waiting plant is refused at 12:59:55 (0 + 1 + 9 + 1 = 11 > 10); the second ends
    # at 13:00:01 without a request, after the slot of 13:00 UTC.
    outcome = revalidate_account(
        system.account.pk,
        utc(D1, 12, 59) + timedelta(seconds=55),
        claim=lease,
        adapter_for=lambda binding: (
            SlowlyFailingPreparation(ticks) if binding.pk == slow.pk else FakeAdapter()
        ),
    )

    assert outcome == WAITING
    assert account(system).revalidation_due_at == utc(D1, 13) + AFTER_THE_SLOT
    assert [revision(refused), revision(slow)] == [1, 1]
    assert used(system, utc(D1, 13)) == 0
