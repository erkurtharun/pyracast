from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from types import SimpleNamespace
from unittest.mock import MagicMock, call

import pytest
from django.contrib.auth.models import User
from django.db import close_old_connections, connection, connections, transaction

from pyracast import runtime
from pyracast.accounts.models import Membership, Tenant
from pyracast.connections.contracts import ProviderError
from pyracast.connections.operations import validate_binding
from pyracast.forecasts.jobs import collect
from pyracast.forecasts.models import CollectionJob, RequestAttempt
from pyracast.forecasts.monitoring import PhaseOutcome
from pyracast.forecasts.scheduler import CollectionSchedulingError, enqueue_due
from pyracast.providers import normalize_payload
from pyracast.regions.models import Region
from pyracast.regions.operations import approve_region, configure_binding, submit_region
from tests.test_domain import NOW, FakeAdapter, make_system
from tests.test_jobs import calculate


def test_existing_pending_collections_are_dispatched_after_scheduler_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(
        runtime, "enqueue_due", MagicMock(side_effect=RuntimeError("private response"))
    )
    monkeypatch.setattr(runtime, "pending_job_ids", MagicMock(return_value=[11, 19]))
    reconnect = MagicMock()
    monkeypatch.setattr(runtime, "close_old_connections", reconnect)
    actor = MagicMock()
    assert runtime.dispatch(actor) == PhaseOutcome(published=2, error="RuntimeError")
    assert actor.send.call_args_list == [call(11), call(19)]
    reconnect.assert_called_once()
    assert "private response" not in caplog.text


@pytest.mark.django_db(transaction=True)
def test_contended_region_does_not_suppress_another_tenants_due_collection(
    caplog: pytest.LogCaptureFixture,
) -> None:
    system = make_system(approved=True)
    member = User.objects.create(username="another-collection-member")
    tenant = Tenant.objects.create(name="Another tenant")
    Membership.objects.create(user=member, tenant=tenant, role=Membership.Role.ADMIN)
    region = submit_region(
        member,
        tenant.pk,
        name="Independent region",
        latitude=41,
        longitude=30,
        timezone="Europe/Istanbul",
    )
    binding = configure_binding(
        system.admin,
        region.pk,
        provider_account_id=system.account.pk,
        site_id="independent-resource",
        capacity_kw=1,
        capacity_semantics_verified=True,
    )
    validate_binding(system.admin, binding.pk, FakeAdapter(), NOW)
    approve_region(system.admin, region.pk, binding.pk, NOW)
    acquired, release = Event(), Event()

    def hold_region() -> None:
        close_old_connections()
        try:
            with transaction.atomic():
                Region.objects.select_for_update().get(pk=system.region.pk)
                acquired.set()
                assert release.wait(timeout=10)
        finally:
            connections.close_all()

    with connection.cursor() as cursor:
        cursor.execute("SHOW lock_timeout")
        original_timeout = cursor.fetchone()
        assert original_timeout is not None
        cursor.execute("SET lock_timeout = '100ms'")
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            holder = pool.submit(hold_region)
            assert acquired.wait(timeout=10)
            try:
                with pytest.raises(CollectionSchedulingError):
                    enqueue_due(NOW + timedelta(days=1))
            finally:
                release.set()
            holder.result(timeout=10)
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config('lock_timeout', %s, false)", [original_timeout[0]])
    assert CollectionJob.objects.filter(
        binding=binding, scheduled_slot=NOW + timedelta(days=1)
    ).exists()
    assert not CollectionJob.objects.filter(
        binding=system.binding, scheduled_slot=NOW + timedelta(days=1)
    ).exists()
    assert "error_type=OperationalError" in caplog.text


@pytest.mark.django_db(transaction=True)
def test_five_minute_provider_outage_recovers_with_the_third_quota_accounted_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    make_system(approved=True)
    job = CollectionJob.objects.get()
    moment = NOW
    monkeypatch.setattr("pyracast.forecasts.jobs.time", SimpleNamespace(monotonic=lambda: 0.0))
    monkeypatch.setattr("pyracast.provider_retry.secrets.randbelow", lambda _: 0)

    def provider() -> None:
        if moment < NOW + timedelta(minutes=5):
            raise ProviderError("private server response", status_code=503)

    assert not collect(job.pk, FakeAdapter(provider), calculate, moment)
    job.refresh_from_db()
    moment = job.available_at
    assert NOW + timedelta(minutes=4) <= moment < NOW + timedelta(minutes=4, seconds=1)
    assert not collect(job.pk, FakeAdapter(provider), calculate, moment)
    job.refresh_from_db()
    moment = job.available_at
    assert NOW + timedelta(minutes=12) <= moment < NOW + timedelta(minutes=12, seconds=1)
    assert collect(job.pk, FakeAdapter(provider), calculate, moment)
    job.refresh_from_db()
    assert (job.status, job.attempts) == ("succeeded", 3)
    assert RequestAttempt.objects.filter(job=job).count() == 3
    assert RequestAttempt.objects.filter(job=job, outcome="failed").count() == 2


@pytest.mark.django_db(transaction=True)
def test_deterministically_invalid_provider_payload_does_not_spend_retry_quota() -> None:
    make_system(approved=True)
    job = CollectionJob.objects.get()

    def malformed() -> None:
        normalize_payload({"forecasts": [{"period": "PT15M"}]})

    assert not collect(job.pk, FakeAdapter(malformed), calculate, NOW)
    job.refresh_from_db()
    assert (job.status, job.attempts) == ("failed", 1)
    assert not collect(job.pk, FakeAdapter(), calculate, NOW + timedelta(minutes=10))
    assert RequestAttempt.objects.filter(job=job).count() == 1
