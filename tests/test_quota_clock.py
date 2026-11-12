from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from types import SimpleNamespace

import pytest
from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection

from pyracast.connections import operations
from pyracast.connections.models import ProviderAccount
from pyracast.connections.operations import (
    QuotaExceededError,
    reserve_account_request,
    reserve_request,
)
from pyracast.forecasts.models import CollectionJob, RequestAttempt
from pyracast.regions.operations import configure_binding
from tests.test_domain import NOW, make_system, secret_ref

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("next_day_full", [False, True])
def test_quota_uses_utc_day_after_account_lock_wait(
    monkeypatch: pytest.MonkeyPatch, next_day_full: bool
) -> None:
    system = make_system()
    ProviderAccount.objects.filter(pk=system.account.pk).update(daily_quota=1)
    started = datetime(2026, 11, 9, 23, 59, 59, tzinfo=UTC)
    reserved = started + timedelta(seconds=2)
    if next_day_full:
        reserve_request(system.binding, reserved, purpose="validation")
    ticks = iter([0.0, 2.0])
    monkeypatch.setattr(operations, "time", SimpleNamespace(monotonic=lambda: next(ticks)))

    if next_day_full:
        with pytest.raises(QuotaExceededError):
            reserve_request(system.binding, started, purpose="forecast")
    else:
        attempt = reserve_request(system.binding, started, purpose="forecast")
        assert attempt.utc_day == reserved.date()
        assert attempt.created_at == reserved
    assert RequestAttempt.objects.count() == 1


def test_prebinding_requests_share_the_account_ledger() -> None:
    system = make_system()
    ProviderAccount.objects.filter(pk=system.account.pk).update(daily_quota=1)
    attempt = reserve_account_request(system.account, NOW, purpose="site_settings")
    assert attempt.binding_id is None
    assert attempt.job_id is None
    assert attempt.outcome == "uncertain"
    assert attempt.provider_account_id == system.account.pk
    with pytest.raises(QuotaExceededError):
        reserve_request(system.binding, NOW, purpose="validation")
    assert RequestAttempt.objects.count() == 1


def test_prebinding_and_forecast_reservations_serialize_one_budget() -> None:
    system = make_system()
    ProviderAccount.objects.filter(pk=system.account.pk).update(daily_quota=1)
    barrier = Barrier(2)

    def reserve(*, before_binding: bool) -> bool:
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            if before_binding:
                reserve_account_request(system.account, NOW, purpose="site_settings")
            else:
                reserve_request(system.binding, NOW, purpose="forecast")
            return True
        except QuotaExceededError:
            return False
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(reserve, before_binding=before) for before in (False, True)]
        assert sorted(future.result(timeout=15) for future in futures) == [False, True]
    assert RequestAttempt.objects.count() == 1


@pytest.mark.parametrize("state", ["disabled", "unauthorized"])
def test_prebinding_reservations_recheck_account_authorization(state: str) -> None:
    system = make_system()
    if state == "disabled":
        ProviderAccount.objects.filter(pk=system.account.pk).update(enabled=False)
    else:
        ProviderAccount.objects.filter(pk=system.account.pk).update(authorized_at=None)
    with pytest.raises(QuotaExceededError):
        reserve_account_request(system.account, NOW, purpose="site_settings")
    assert not RequestAttempt.objects.exists()


def test_reservations_reject_mismatched_account_binding_and_job() -> None:
    system = make_system()
    other_account = ProviderAccount.objects.create(
        provider="fixture", external_account_id="another", secret_reference=secret_ref("ANOTHER")
    )
    with pytest.raises(ValidationError, match="provider account"):
        reserve_account_request(other_account, NOW, purpose="forecast", binding=system.binding)
    job = CollectionJob.objects.create(
        binding=system.binding,
        scheduled_slot=NOW,
        target_date=NOW.date(),
        available_at=NOW,
    )
    with pytest.raises(ValidationError, match="job must belong"):
        reserve_account_request(system.account, NOW, purpose="forecast", job=job)
    replacement = configure_binding(
        system.admin,
        system.region.pk,
        provider_account_id=system.account.pk,
        site_id="replacement",
        capacity_kw=1,
        capacity_semantics_verified=True,
    )
    with pytest.raises(ValidationError, match="job must belong"):
        reserve_account_request(
            system.account, NOW, purpose="forecast", binding=replacement, job=job
        )
    assert not RequestAttempt.objects.exists()
