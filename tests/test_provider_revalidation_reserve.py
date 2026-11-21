from datetime import UTC, datetime, timedelta

import pytest

from pyracast.connections.models import ProviderAccount, SiteBinding
from pyracast.forecasts.budget import remaining_forecast_requests
from pyracast.forecasts.models import CollectionJob, CollectionSchedule, RequestAttempt
from pyracast.forecasts.quota import ForecastBudgetReservedError
from pyracast.forecasts.revalidation import RevalidationOutcome, revalidate_account
from pyracast.forecasts.validation import validate_binding
from pyracast.plants.approval import approve_region
from pyracast.platform_admin.provider_accounts import account_context
from pyracast.regions.operations import configure_binding, submit_region
from tests.support.domain import NOW, FakeAdapter, System, make_system, provider_key
from tests.support.providers import replace_key

pytestmark = pytest.mark.django_db(transaction=True)
NEXT_UTC_DAY = datetime.combine(NOW.date() + timedelta(days=1), datetime.min.time(), UTC)


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
    assert remaining_forecast_requests(account(system), NOW) == 1
    assert account_context(account(system), NOW)["budget"].reserved == 1


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


def test_the_first_validation_of_a_waiting_plant_is_not_held_back_by_the_reserve() -> None:
    system = collecting_system()
    waiting = another_plant(system, "waiting-site", approved=False)
    leave_requests(system, 1)
    assert remaining_forecast_requests(account(system), NOW) == 1

    validate_binding(system.admin, waiting.pk, FakeAdapter(), NOW)

    waiting.refresh_from_db()
    assert waiting.validated_credential_revision == 1
