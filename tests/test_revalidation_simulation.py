from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from pyracast.connections.contracts import ProviderData, ProviderError
from pyracast.connections.models import ProviderAccount, SiteBinding
from pyracast.forecasts.budget import (
    forecast_starts,
    remaining_forecast_requests,
    slot_day,
    started_jobs,
)
from pyracast.forecasts.models import CollectionJob, CollectionSchedule, RequestAttempt
from pyracast.forecasts.revalidation import revalidate_account, stale_bindings
from pyracast.forecasts.timing import forecast_timing
from pyracast.local_time import local_slot
from pyracast.provider_retry import next_provider_day
from tests.support.collection_schedule import HistoricalFixture
from tests.support.domain import NOW, FakeAdapter, System, provider_key
from tests.support.providers import replace_key
from tests.support.revalidation import (
    ROUND_OFFSETS,
    account,
    assert_current_plants_keep_their_forecasts,
    assert_the_step_kept_the_reserve,
    collecting_account,
    current_plants,
    dispatch_round,
    last_request,
    spend_until,
    used,
    utc,
)

pytestmark = pytest.mark.django_db(transaction=True)
D1, D2, D3 = (NOW.date() + timedelta(days=offset) for offset in range(1, 4))
END = utc(D3 + timedelta(days=1), 0)
RETRY_AFTER = timedelta(hours=2)


@dataclass(frozen=True)
class Ending:
    """How a plant's forecast job of a slot ends, and the requests it sent."""

    plant: int
    slot: datetime
    status: str
    last_error: str
    requests: int


@dataclass
class Case:
    """`lost` names the forecast job a provider failure on a fully booked day delays to the next
    provider day and the moment it is refused for the used-up quota; `endings` are the jobs that
    failure touches; `reopened` are the jobs refused for the revoked key that the revalidation
    reopens."""

    timezone: str
    clocks: list[list[str]]
    quota: int
    replaced_at: datetime
    revoked_at: datetime | None = None
    failing_at: datetime | None = None
    lost: tuple[int, datetime, datetime] | None = None
    endings: tuple[Ending, ...] = ()
    reopened: tuple[Ending, ...] = ()
    failed: bool = field(default=False, init=False)
    refused: bool = field(default=False, init=False)


class Provider(HistoricalFixture):
    """Refuses the first key from `revoked_at` on, and answers the first forecast request at
    `failing_at` with a server error and a Retry-After of two hours."""

    def __init__(self, case: Case, moment: datetime) -> None:
        super().__init__(moment)
        self.case = case

    def _refuse_a_revoked_key(self, binding: SiteBinding) -> None:
        revision = ProviderAccount.objects.get(pk=binding.provider_account_id).credential_revision
        if self.case.revoked_at is not None and self.now >= self.case.revoked_at and revision == 1:
            self.case.refused = True
            raise ProviderError("provider_request_failed", status_code=401, retryable=False)

    def fetch(self, binding: SiteBinding, target_date: date) -> ProviderData:
        self._refuse_a_revoked_key(binding)
        if self.now == self.case.failing_at and not self.case.failed:
            self.case.failed = True
            raise ProviderError(
                "provider_request_failed",
                status_code=503,
                retry_after_seconds=int(RETRY_AFTER.total_seconds()),
            )
        return super().fetch(binding, target_date)

    def fetch_history(self, binding: SiteBinding, target_date: date, days: int) -> ProviderData:
        self._refuse_a_revoked_key(binding)
        return super().fetch_history(binding, target_date, days)


EAST_CLOCKS = [["06:00", "16:00"], ["16:00"], ["09:30"], ["06:00", "21:00"]]
CASES = [
    # East of UTC, key replaced before the day's slots: 6 clocks and 4 waiting plants on 10. The
    # provider fails the first plant's forecast of D2 13:00 UTC; its retry takes the day's last
    # request, so the fourth plant's forecast of 18:00 UTC is refused for the used-up quota and
    # sent at the next provider day's start.
    pytest.param(
        Case(
            "Europe/Istanbul",
            EAST_CLOCKS,
            10,
            utc(D1, 0, 30),
            failing_at=utc(D2, 13),
            lost=(3, utc(D2, 18), utc(D2, 18)),
            endings=(
                Ending(0, utc(D2, 13), "succeeded", "", 2),
                Ending(3, utc(D2, 18), "succeeded", "", 1),
            ),
        ),
        id="east-replaced-before-the-slots",
    ),
    # As above, with the provider failing the day's last forecast, of 18:00 UTC: its retry is
    # refused for the used-up quota and sent at the next provider day's start.
    pytest.param(
        Case(
            "Europe/Istanbul",
            EAST_CLOCKS,
            10,
            utc(D1, 0, 30),
            failing_at=utc(D2, 18),
            lost=(3, utc(D2, 18), utc(D2, 20, 2)),
            endings=(Ending(3, utc(D2, 18), "succeeded", "", 2),),
        ),
        id="east-failed-on-the-last-slot",
    ),
    # West of UTC, the key revoked before the 11:00 UTC slot and replaced before 23:45 UTC:
    # 6 clocks and 4 waiting plants on 10.
    pytest.param(
        Case(
            "America/New_York",
            [["06:00", "18:45"], ["06:00", "18:45"], ["18:45"], ["18:45"]],
            10,
            utc(D1, 15),
            revoked_at=utc(D1, 10),
            reopened=(
                Ending(0, utc(D1, 11), "succeeded", "", 1),
                Ending(1, utc(D1, 11), "succeeded", "", 1),
                Ending(2, utc(D1 - timedelta(days=1), 23, 45), "succeeded", "", 1),
            ),
        ),
        id="west-replaced-between-the-slots",
    ),
    # A forecast refused for the revoked key a day before the replacement: 1 clock and 1 waiting
    # plant on 2.
    pytest.param(
        Case("Europe/Istanbul", [["16:00"]], 2, utc(D2, 12), revoked_at=utc(D1, 12)),
        id="refused-a-day-before-the-replacement",
    ),
    # East of UTC, key replaced after the day's first slots: 3 clocks and 3 waiting plants on 6.
    pytest.param(
        Case("Asia/Tokyo", [["09:00"], ["10:00"], ["08:00"]], 6, utc(D1, 20)),
        id="east-replaced-after-the-slots",
    ),
]


def plant_slots(case: Case, clocks: list[str]) -> set[datetime]:
    zone = ZoneInfo(case.timezone)
    days = [D1 - timedelta(days=1), D1, D2, D3, D3 + timedelta(days=1)]
    return {
        slot
        for day in days
        for clock in clocks
        if (slot := local_slot(day, clock, zone)) is not None and utc(D1, 0) <= slot < END
    }


def slots(case: Case) -> list[datetime]:
    """The forecast slots of the simulated days, the moment a failed forecast retries and the
    start of each provider day after D1, when a forecast refused for the used-up quota is ready
    again. D1 starts at its first slot, which collects the setup's backlog of D0."""
    return sorted(
        plant_slots(case, [clock for clocks in case.clocks for clock in clocks])
        | ({case.failing_at + RETRY_AFTER} if case.failing_at is not None else set())
        | {next_provider_day(utc(day, 0)) for day in (D1, D2)}
    )


def assert_every_slot_after_a_carried_forecast_is_collected_at_its_time(
    plants: list[SiteBinding], case: Case, awaiting: set[tuple[int, datetime]]
) -> None:
    """A forecast job first sent on a later UTC day than its slot's leaves its plant every slot
    from that day on, each collected in its own rounds. Excused are the jobs the case's provider
    failure touches and slots whose rounds end while the plant awaits revalidation (`awaiting`
    holds the plant and round moments it was not current at)."""
    excused = {(plants[ending.plant].pk, ending.slot) for ending in case.endings}
    closing = max(ROUND_OFFSETS)
    for binding, clocks in zip(plants, case.clocks, strict=True):
        first: dict[int, tuple[datetime, datetime]] = {}
        for job_id, slot, made in (
            RequestAttempt.objects.filter(binding=binding, purpose="forecast", job__isnull=False)
            .order_by("created_at", "pk")
            .values_list("job_id", "job__scheduled_for", "created_at")
        ):
            first.setdefault(job_id, (slot, made))
        carried = [made for slot, made in first.values() if made.date() > slot.date()]
        if not carried:
            continue
        since = utc(min(carried).date(), 0)
        for slot in sorted(plant_slots(case, clocks)):
            if (
                slot < since
                or slot + closing >= END
                or (binding.pk, slot) in excused
                or (binding.pk, slot + closing) in awaiting
            ):
                continue
            job = CollectionJob.objects.get(
                binding=binding, endpoint="forecasts", scheduled_for=slot
            )
            assert job.status == "succeeded", (binding.pk, slot, job.status, job.last_error)
            assert job.pk in first, (binding.pk, slot)
            assert slot <= first[job.pk][1] <= slot + closing, (binding.pk, slot, first[job.pk])


def assert_no_forecast_was_refused_for_a_used_up_quota(
    system: System,
    plants: list[SiteBinding],
    case: Case,
    moment: datetime,
    refused: dict[int, datetime],
) -> None:
    """A forecast refused for the used-up quota is allowed only where claim's day bound refuses
    it anyway (a job not started before, whose plant's starts of its slot's UTC day are used up),
    or as the one forecast the case's provider failure delays that provider day; none is refused
    for the reserve, which holds back other requests only."""
    assert not CollectionJob.objects.filter(
        binding__provider_account=system.account,
        endpoint="forecasts",
        last_error="history_budget_reserved",
    ).exists(), moment
    jobs = CollectionJob.objects.filter(
        binding__provider_account=system.account,
        endpoint="forecasts",
        status="retry",
        last_error="quota_exhausted",
    ).exclude(pk__in=refused)
    for job in jobs.select_related("binding__provider_account"):
        refused[job.pk] = moment
        if (
            case.lost is not None
            and case.failing_at is not None
            and (job.binding_id, job.scheduled_for) == (plants[case.lost[0]].pk, case.lost[1])
            and moment.date() == case.failing_at.date()
        ):
            continue
        timing = forecast_timing(
            job.binding,
            CollectionSchedule.objects.filter(region_id=job.binding.region_id).first(),
            daily_quota=job.binding.provider_account.daily_quota,
        )
        assert timing is not None
        assert job.pk not in started_jobs(timing, [job.pk]), (job.scheduled_for, moment)
        assert forecast_starts(job.binding.pk, timing, slot_day(job.scheduled_for)) >= len(
            timing.times
        ), (
            job.scheduled_for,
            moment,
        )


def assert_no_provider_day_exceeds_the_quota(system: System) -> None:
    days = Counter(
        RequestAttempt.objects.filter(provider_account=system.account).values_list(
            "utc_day", flat=True
        )
    )
    assert max(days.values(), default=0) <= account(system).daily_quota, days


def assert_the_jobs_ended(plants: list[SiteBinding], endings: tuple[Ending, ...]) -> None:
    for ending in endings:
        job = CollectionJob.objects.get(
            binding=plants[ending.plant], endpoint="forecasts", scheduled_for=ending.slot
        )
        assert (job.status, job.last_error, RequestAttempt.objects.filter(job=job).count()) == (
            ending.status,
            ending.last_error,
            ending.requests,
        ), ending


def assert_the_failure_cost_one_forecast(
    system: System, plants: list[SiteBinding], case: Case, refused: dict[int, datetime]
) -> None:
    if case.failing_at is None:
        return
    assert case.lost is not None
    assert used(system, case.failing_at) == case.quota
    index, slot, refused_at = case.lost
    lost = CollectionJob.objects.get(
        binding=plants[index], endpoint="forecasts", scheduled_for=slot
    )
    assert refused.get(lost.pk) == refused_at
    assert_the_jobs_ended(plants, case.endings)


@pytest.mark.parametrize("case", CASES)
def test_the_scheduler_claim_and_revalidation_keep_the_reserve_over_three_provider_days(
    case: Case,
) -> None:
    system, plants = collecting_account(case.clocks, replaced=False, timezone=case.timezone)
    ProviderAccount.objects.filter(pk=system.account.pk).update(daily_quota=case.quota)
    rounds = sorted({slot + offset for slot in slots(case) for offset in ROUND_OFFSETS})
    run_at: datetime | None = None
    claim: datetime | None = None
    replaced = False
    revalidated_at: datetime | None = None
    refused: dict[int, datetime] = {}
    awaiting: set[tuple[int, datetime]] = set()
    while True:
        due = [
            *rounds[:1],
            *([run_at] if run_at is not None else []),
            *([] if replaced else [case.replaced_at]),
        ]
        moment = min(due, default=END)
        if moment >= END:
            break
        since = last_request()
        if not replaced and moment == case.replaced_at:
            replace_key(system.admin, system.account.pk, provider_key("NEW"))
            replaced = True
            claim = account(system).revalidation_due_at
            run_at = moment
        elif rounds and rounds[0] == moment:
            current = {binding.pk for binding in current_plants(system)}
            awaiting |= {(binding.pk, moment) for binding in plants if binding.pk not in current}
            dispatch_round(rounds.pop(0), Provider(case, moment))
        else:
            assert claim is not None
            revalidate_account(
                system.account.pk, moment, claim=claim, adapter_for=lambda binding: FakeAdapter()
            )
            dispatch_round(moment, Provider(case, moment))
            rounds = sorted({*rounds, *(moment + offset for offset in ROUND_OFFSETS[1:])})
            claim = run_at = account(system).revalidation_due_at
            if revalidated_at is None and not stale_bindings(account(system)).exists():
                revalidated_at = moment
        assert_the_step_kept_the_reserve(system, moment, since)
        assert_no_forecast_was_refused_for_a_used_up_quota(system, plants, case, moment, refused)
        assert_no_provider_day_exceeds_the_quota(system)

    assert_every_slot_after_a_carried_forecast_is_collected_at_its_time(plants, case, awaiting)
    assert replaced
    if sum(map(len, case.clocks)) + len(plants) <= case.quota:
        assert revalidated_at is not None
        assert revalidated_at < utc(case.replaced_at.date() + timedelta(days=2), 0)
    assert case.failing_at is None or case.failed
    assert case.revoked_at is None or case.refused
    assert_the_failure_cost_one_forecast(system, plants, case, refused)
    assert_the_jobs_ended(plants, case.reopened)


def collected_with_a_server_error(clocks: list[str], slots: list[datetime]) -> System:
    """One current plant on quota 10 whose slots are collected while the provider fails the
    forecast request of the last of them."""
    system, _plants = collecting_account([clocks], replaced=False)
    failing = Case("Europe/Istanbul", [], 10, END, failing_at=slots[-1])
    for moment in sorted(slot + offset for slot in slots for offset in ROUND_OFFSETS):
        dispatch_round(moment, Provider(failing, moment))
    return system


def test_the_reserve_check_fails_a_history_request_that_took_a_retrys_room() -> None:
    system = collected_with_a_server_error(["06:00", "16:00"], [utc(D1, 3), utc(D1, 13)])
    moment = utc(D1, 13, 30)
    assert (used(system, moment), remaining_forecast_requests(account(system), moment)) == (5, 1)
    since = last_request()

    spend_until(system, moment, 10)

    # 5 used and the retry left 4 on 10; history requests took 5, the retry's room among them.
    with pytest.raises(AssertionError):
        assert_the_step_kept_the_reserve(system, moment, since)
    with pytest.raises(AssertionError):
        assert_current_plants_keep_their_forecasts(system, moment)


def test_the_reserve_check_holds_no_room_for_a_failed_forecast_a_later_slot_superseded() -> None:
    system = collected_with_a_server_error(["06:00", "12:00", "16:00"], [utc(D1, 3), utc(D1, 9)])
    moment = utc(D1, 13)
    spend_until(system, moment, 10)
    since = last_request()

    # The job of 09:00 UTC still waits for its retry, but claim ends it for the slot of 13:00, due
    # now and alone still needed. No failed job is sent again: 10 + 1 > 10 has no excuse.
    failed = CollectionJob.objects.get(
        binding=system.binding, endpoint="forecasts", scheduled_for=utc(D1, 9)
    )
    assert (failed.status, failed.last_error) == ("retry", "provider_server_error")
    assert remaining_forecast_requests(account(system), moment) == 1
    with pytest.raises(AssertionError):
        assert_the_step_kept_the_reserve(system, moment, since)
