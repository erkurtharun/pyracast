from datetime import timedelta

import pytest

from pyracast.connections import operations
from pyracast.connections.models import ProviderAccount
from pyracast.connections.operations import ForecastBudgetReservedError, validate_binding
from pyracast.forecasts.models import RequestAttempt
from tests.support.domain import NOW, FakeAdapter, make_system

pytestmark = pytest.mark.django_db(transaction=True)


def leave_one_request_for(remaining_forecasts: int, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        operations, "remaining_forecast_requests", lambda account, now: remaining_forecasts
    )


def test_revalidating_a_collecting_plant_never_takes_a_request_a_forecast_needs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = make_system(approved=True)
    ProviderAccount.objects.filter(pk=system.account.pk).update(daily_quota=2)
    leave_one_request_for(1, monkeypatch)
    attempts = RequestAttempt.objects.count()

    with pytest.raises(ForecastBudgetReservedError):
        validate_binding(system.admin, system.binding.pk, FakeAdapter(), NOW)

    assert RequestAttempt.objects.count() == attempts


def test_revalidating_a_collecting_plant_spends_what_the_forecasts_leave(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = make_system(approved=True)
    ProviderAccount.objects.filter(pk=system.account.pk).update(daily_quota=3)
    leave_one_request_for(1, monkeypatch)

    validate_binding(system.admin, system.binding.pk, FakeAdapter(), NOW)

    assert RequestAttempt.objects.filter(purpose="validation", utc_day=NOW.date()).count() == 2


def test_the_first_validation_of_a_waiting_plant_is_not_held_back_by_the_reserve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = make_system()
    ProviderAccount.objects.filter(pk=system.account.pk).update(daily_quota=1)
    leave_one_request_for(1, monkeypatch)

    validate_binding(system.admin, system.binding.pk, FakeAdapter(), NOW - timedelta(hours=1))

    assert RequestAttempt.objects.filter(purpose="validation").count() == 1
