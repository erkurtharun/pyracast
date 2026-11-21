from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from pyracast.connections.contracts import ProviderData, ProviderError
from pyracast.connections.models import ProviderAccount, SiteBinding
from pyracast.forecasts.budget import forecast_starts
from pyracast.forecasts.models import CollectionJob, CollectionSchedule, RequestAttempt
from pyracast.forecasts.revalidation import revalidate_account, stale_bindings
from pyracast.forecasts.timing import forecast_timing
from pyracast.local_time import local_slot
from tests.support.collection_schedule import HistoricalFixture
from tests.support.domain import NOW, FakeAdapter, System, provider_key
from tests.support.providers import replace_key
from tests.support.revalidation import (
    ROUND_OFFSETS,
    account,
    assert_current_plants_keep_their_forecasts,
    collecting_account,
    dispatch_round,
    utc,
)

pytestmark = pytest.mark.django_db(transaction=True)
D1, D2, D3 = (NOW.date() + timedelta(days=offset) for offset in range(1, 4))
END = utc(D3 + timedelta(days=1), 0)


@dataclass
class Case:
    timezone: str
    clocks: list[list[str]]
    quota: int
    replaced_at: datetime
    revoked_at: datetime | None = None
    failing_at: datetime | None = None
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
                "provider_request_failed", status_code=503, retry_after_seconds=7200
            )
        return super().fetch(binding, target_date)

    def fetch_history(self, binding: SiteBinding, target_date: date, days: int) -> ProviderData:
        self._refuse_a_revoked_key(binding)
        return super().fetch_history(binding, target_date, days)


CASES = [
    # East of UTC, key replaced before the day's slots: 6 clocks and 4 waiting plants on 10.
    pytest.param(
        Case(
            "Europe/Istanbul",
            [["06:00", "16:00"], ["16:00"], ["09:30"], ["06:00", "21:00"]],
            10,
            utc(D1, 0, 30),
            failing_at=utc(D2, 13),
        ),
        id="east-replaced-before-the-slots",
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


def slots(case: Case) -> list[datetime]:
    zone = ZoneInfo(case.timezone)
    days = [D1 - timedelta(days=1), D1, D2, D3, D3 + timedelta(days=1)]
    return sorted(
        {
            slot
            for day in days
            for clock in {clock for clocks in case.clocks for clock in clocks}
            if (slot := local_slot(day, clock, zone)) is not None and utc(D1, 0) <= slot < END
        }
    )


def assert_no_forecast_was_refused_for_a_used_up_quota(
    system: System, moment: datetime, checked: set[int]
) -> None:
    """A forecast refused for the used-up quota is allowed only where its plant's starts of the
    day are used up as well (claim's day bound refuses it anyway); none is refused for the
    reserve, which holds back other requests only."""
    assert not CollectionJob.objects.filter(
        binding__provider_account=system.account,
        endpoint="forecasts",
        last_error="history_budget_reserved",
    ).exists(), moment
    refused = CollectionJob.objects.filter(
        binding__provider_account=system.account,
        endpoint="forecasts",
        status="retry",
        last_error="quota_exhausted",
    ).exclude(pk__in=checked)
    for job in refused.select_related("binding__provider_account"):
        timing = forecast_timing(
            job.binding, CollectionSchedule.objects.filter(region_id=job.binding.region_id).first()
        )
        assert timing is not None
        assert forecast_starts(job.binding.pk, timing, moment.date()) >= len(timing.times), (
            job.scheduled_for,
            moment,
        )
        checked.add(job.pk)


def assert_no_provider_day_exceeds_the_quota(system: System) -> None:
    days = Counter(
        RequestAttempt.objects.filter(provider_account=system.account).values_list(
            "utc_day", flat=True
        )
    )
    assert max(days.values(), default=0) <= account(system).daily_quota, days


@pytest.mark.parametrize("case", CASES)
def test_the_scheduler_claim_and_revalidation_keep_the_reserve_over_three_provider_days(
    case: Case,
) -> None:
    system, plants = collecting_account(case.clocks, replaced=False, timezone=case.timezone)
    ProviderAccount.objects.filter(pk=system.account.pk).update(daily_quota=case.quota)
    rounds = sorted({slot + offset for slot in slots(case) for offset in ROUND_OFFSETS})
    run_at: datetime | None = None
    claim: datetime | None = None
    revalidated_at: datetime | None = None
    checked: set[int] = set()
    while True:
        moments = [
            *rounds[:1],
            *([run_at] if run_at is not None else []),
            *([case.replaced_at] if claim is None else []),
        ]
        moment = min(moments)
        if moment >= END:
            break
        if claim is None and moment == case.replaced_at:
            replace_key(system.admin, system.account.pk, provider_key("NEW"))
            claim = account(system).revalidation_due_at
            run_at = moment
        elif rounds and rounds[0] == moment:
            dispatch_round(rounds.pop(0), Provider(case, moment))
        else:
            assert claim is not None
            revalidate_account(
                system.account.pk, moment, claim=claim, adapter_for=lambda binding: FakeAdapter()
            )
            dispatch_round(moment, Provider(case, moment))
            claim = run_at = account(system).revalidation_due_at
            if revalidated_at is None and not stale_bindings(account(system)).exists():
                revalidated_at = moment
        assert_current_plants_keep_their_forecasts(system, moment)
        assert_no_forecast_was_refused_for_a_used_up_quota(system, moment, checked)
        assert_no_provider_day_exceeds_the_quota(system)

    if sum(map(len, case.clocks)) + len(plants) <= case.quota:
        assert revalidated_at is not None
        assert revalidated_at < utc(case.replaced_at.date() + timedelta(days=2), 0)
    assert case.failing_at is None or case.failed
    assert case.revoked_at is None or case.refused
