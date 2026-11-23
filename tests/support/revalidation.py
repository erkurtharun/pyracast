from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from django.db.models import Max, Q

from pyracast.connections.models import ProviderAccount, SiteBinding
from pyracast.forecasts import budget
from pyracast.forecasts.jobs.ready import pending_job_ids
from pyracast.forecasts.models import CollectionJob, CollectionSchedule, RequestAttempt
from pyracast.forecasts.revalidation import RevalidationOutcome, revalidate_account
from pyracast.forecasts.scheduler import enqueue_due
from pyracast.forecasts.timing import forecast_timing
from pyracast.forecasts.validation import validate_binding
from pyracast.plants.approval import approve_region
from pyracast.regions.models import Region
from pyracast.regions.operations import configure_binding, submit_region
from tests.support.collection_schedule import HistoricalFixture
from tests.support.domain import NOW, FakeAdapter, System, make_system, provider_key
from tests.support.jobs import calculate, collect
from tests.support.providers import replace_key

QUOTA = 10
# Rounds after a slot in which the dispatcher collects the slot's jobs and their retries.
ROUND_OFFSETS = (timedelta(0), timedelta(minutes=2), timedelta(minutes=6))


def utc(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), UTC)


def plant(
    system: System,
    index: int,
    times: list[str],
    *,
    validated: datetime,
    timezone: str = "Europe/Istanbul",
) -> SiteBinding:
    name = f"site-{index}"
    region = submit_region(
        system.member,
        system.tenant.pk,
        name=name,
        latitude=41,
        longitude=30 + index / 100,
        timezone=timezone,
    )
    binding = configure_binding(
        system.admin,
        region.pk,
        provider_account_id=system.account.pk,
        site_id=name,
        capacity_kw=1,
        capacity_semantics_verified=True,
    )
    validate_binding(system.admin, binding.pk, FakeAdapter(), validated)
    approve_region(system.admin, region.pk, binding.pk, NOW)
    CollectionSchedule.objects.create(
        region=region, times=times, effective_from=NOW - timedelta(days=1)
    )
    return binding


def collecting_account(
    clocks: Iterable[list[str]], *, replaced: bool = True, timezone: str = "Europe/Istanbul"
) -> tuple[System, list[SiteBinding]]:
    """Plants approved and collected at NOW, the first of them the system's own."""
    system = make_system(approved=True)
    Region.objects.filter(pk=system.region.pk).update(timezone=timezone)
    SiteBinding.objects.filter(pk=system.binding.pk).update(timezone=timezone)
    system.region.refresh_from_db()
    system.binding.refresh_from_db()
    schedules = list(clocks)
    CollectionSchedule.objects.create(
        region=system.region, times=schedules[0], effective_from=NOW - timedelta(days=1)
    )
    plants = [system.binding]
    for index, times in enumerate(schedules[1:], start=2):
        validated = NOW - timedelta(hours=1) - (timedelta(days=1) if index > 5 else timedelta(0))
        plants.append(plant(system, index, times, validated=validated, timezone=timezone))
    CollectionJob.objects.update(status="succeeded")
    ProviderAccount.objects.filter(pk=system.account.pk).update(daily_quota=QUOTA)
    if replaced:
        replace_key(system.admin, system.account.pk, provider_key("NEW"))
    return system, plants


def account(system: System) -> ProviderAccount:
    return ProviderAccount.objects.get(pk=system.account.pk)


def used(system: System, moment: datetime) -> int:
    return RequestAttempt.objects.filter(
        provider_account=system.account, utc_day=moment.astimezone(UTC).date()
    ).count()


def spend_until(system: System, moment: datetime, total: int) -> None:
    for _ in range(total - used(system, moment)):
        RequestAttempt.objects.create(
            provider_account=system.account,
            utc_day=moment.astimezone(UTC).date(),
            created_at=moment,
            purpose="estimated_actuals",
        )


def current_plants(system: System) -> list[SiteBinding]:
    revision = account(system).credential_revision
    return [
        binding
        for binding in budget.approved_bindings(account(system)).select_related(
            "region", "provider_account"
        )
        if binding.validated_credential_revision == revision
    ]


def last_request() -> int:
    return RequestAttempt.objects.aggregate(last=Max("pk"))["last"] or 0


def resent_after_a_provider_failure(plants: list[SiteBinding], moment: datetime, quota: int) -> int:
    """Requests beyond the one held for each forecast job the provider failed (a server error or
    no answer) in the UTC day: its requests of the day plus the one the budget still counts for
    it. A job answered, superseded or off the schedule since its failure adds only a request it
    really sent again."""
    day = moment.astimezone(UTC).date()
    forecasts = RequestAttempt.objects.filter(
        binding__in=plants, purpose="forecast", utc_day=day, job__isnull=False
    )
    failed = set(
        forecasts.filter(
            Q(http_status__gte=500) | Q(http_status__isnull=True), outcome="failed"
        ).values_list("job_id", flat=True)
    )
    counted: set[int] = set()
    for binding in plants:
        schedule = CollectionSchedule.objects.filter(region_id=binding.region_id).first()
        timing = forecast_timing(binding, schedule, daily_quota=quota)
        if timing is not None:
            sent = budget._jobs_still_sent(binding, timing, moment, scheduled=schedule is not None)
            counted |= {job.pk for job in sent.values() if job is not None}
    requests = Counter(forecasts.filter(job_id__in=failed).values_list("job_id", flat=True))
    return sum(requests[job] + (job in counted) - 1 for job in failed)


def assert_current_plants_keep_their_forecasts(system: System, moment: datetime) -> None:
    quota = account(system).daily_quota
    need = sum(budget.forecast_need(binding, moment, quota) for binding in current_plants(system))
    assert used(system, moment) + need <= quota, moment


def assert_the_step_kept_the_reserve(system: System, moment: datetime, since: int) -> None:
    """After a step whose requests come after `since`: one that admitted a validation or a
    history request left the current plants' forecasts their room; one that sent forecasts only
    exceeds it by no more than the requests provider failures of the day made a job send again."""
    quota = account(system).daily_quota
    plants = current_plants(system)
    need = sum(budget.forecast_need(binding, moment, quota) for binding in plants)
    admitted = (
        RequestAttempt.objects.filter(provider_account=system.account, pk__gt=since)
        .exclude(purpose="forecast")
        .exists()
    )
    allowed = 0 if admitted else resent_after_a_provider_failure(plants, moment, quota)
    assert used(system, moment) + need <= quota + allowed, (moment, admitted)


def dispatch_round(moment: datetime, provider: HistoricalFixture | None = None) -> None:
    """One dispatcher round as forecasts.runtime runs it, with the worker collecting each job."""
    enqueue_due(moment)
    for job_id in pending_job_ids(moment):
        collect(job_id, provider or HistoricalFixture(moment), calculate, moment)


@dataclass(frozen=True)
class Run:
    at: datetime
    outcome: RevalidationOutcome
    due: datetime | None
    current: int


def run_revalidations(
    system: System, slots: Iterable[datetime], start: datetime, until: datetime
) -> list[Run]:
    """Runs each revalidation when it is due and the dispatcher at every forecast slot and after
    each revalidation."""
    rounds = sorted(slot + offset for slot in slots for offset in ROUND_OFFSETS)
    claim = account(system).revalidation_due_at
    at: datetime | None = start
    runs: list[Run] = []
    while claim is not None and at is not None and at < until:
        while rounds and rounds[0] <= at:
            moment = rounds.pop(0)
            dispatch_round(moment)
            assert_current_plants_keep_their_forecasts(system, moment)
        outcome = revalidate_account(
            system.account.pk, at, claim=claim, adapter_for=lambda binding: FakeAdapter()
        )
        assert_current_plants_keep_their_forecasts(system, at)
        dispatch_round(at)
        assert_current_plants_keep_their_forecasts(system, at)
        rounds = sorted({*rounds, *(at + offset for offset in ROUND_OFFSETS[1:])})
        claim = account(system).revalidation_due_at
        runs.append(Run(at, outcome, claim, len(current_plants(system))))
        at = claim
    for moment in rounds:
        if moment < until:
            dispatch_round(moment)
            assert_current_plants_keep_their_forecasts(system, moment)
    return runs


def slots_of(days: Iterable[date], hours: Iterable[int]) -> list[datetime]:
    return [utc(day, hour) for day in days for hour in hours]
