from datetime import timedelta

import pytest
from django.utils import timezone

from pyracast.admin_workspace import operations_context, plant_list_context, recent_work_context
from pyracast.forecasts.models import DispatcherPhase
from pyracast.forecasts.monitoring import record_dispatch_phase
from pyracast.metering.models import EpiasCollectionJob
from pyracast.notifications.models import NotificationDelivery, ReminderSchedule
from pyracast.notifications.scheduler import PREPARATION_GRACE
from tests.support.api import verified_admin
from tests.support.delivery_health import NOW, recent_forecast
from tests.support.metering import connected

pytestmark = pytest.mark.django_db(transaction=True)


def test_historical_health_reasons_are_visible_and_link_to_the_matching_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(timezone, "now", lambda: NOW)
    system, plant, _ = connected(monkeypatch)
    recent_forecast(system)
    for phase in DispatcherPhase.values:
        record_dispatch_phase(phase, NOW)
    EpiasCollectionJob.objects.update(status="succeeded")
    failed = EpiasCollectionJob.objects.first()
    assert failed is not None
    failed.status = "failed"
    failed.scheduled_for = NOW - timedelta(hours=2)
    failed.last_error = "upstream_error"
    failed.save()
    for index in range(22):
        EpiasCollectionJob.objects.create(
            connection=failed.connection,
            plant=plant,
            configuration=failed.configuration,
            connection_revision=failed.connection_revision,
            provider_plant=failed.provider_plant,
            target_day=failed.target_day,
            scheduled_for=NOW - timedelta(seconds=index),
            next_attempt_at=NOW,
            status="succeeded",
        )
    schedule = ReminderSchedule.objects.create(region=system.region, effective_from=NOW)
    delivery = NotificationDelivery.objects.create(
        schedule=schedule,
        binding=system.binding,
        scheduled_for=NOW,
        preparation_deadline=NOW + PREPARATION_GRACE,
        available_at=NOW,
        recipients=["recipient@example.test"],
        status="sent",
        prepared_message={"kind": "saved_partial", "tomorrow_complete": False},
    )
    client, _ = verified_admin()
    overview = client.get("/admin/")
    summary = overview.context["health_summary"]
    assert summary.tone == "review"
    assert {check.key for check in summary.checks} == {"failed_metering", "partial_forecasts"}
    assert overview.context["health"]["healthy"] is False
    text = overview.content.decode()
    assert "Recent outcomes need review" in text
    assert "Failed production collection" in text
    assert "Notifications prepared with partial forecasts" in text
    assert "Scheduled in the last 24 hours" in text
    for check in summary.checks:
        assert check.action_url in text
        response = client.get(check.action_url)
        assert response.status_code == 200
        assert response.context["health_filter"].key == check.key
    filtered = client.get("/admin/operations/?issue=failed_metering")
    assert [row["id"] for row in filtered.context["metering"]] == [failed.pk]
    assert filtered.context["show_metering"] is True
    assert filtered.context["show_deliveries"] is False
    assert "Upstream error" in filtered.content.decode()
    assert f"/admin/plants/{system.region.pk}/#epias-title" in filtered.content.decode()
    assert failed.pk not in [row["id"] for row in operations_context()["metering"]]
    email_view = client.get("/admin/operations/?issue=partial_forecasts")
    assert [row["id"] for row in email_view.context["deliveries"]] == [delivery.pk]
    assert "Partial forecast" in email_view.content.decode()
    assert email_view.context["show_collections"] is False
    assert client.get("/admin/operations/?issue=unknown").context["health_filter"] is None
    assert operations_context("dispatch")["health_filter"] is None


def test_health_filter_lists_only_affected_plants_and_preserves_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(timezone, "now", lambda: NOW)
    system, _, _ = connected(monkeypatch)
    recent_forecast(system)
    system.account.authentication_failed_revision = system.account.credential_revision
    system.account.save(update_fields=["authentication_failed_revision"])
    client, _ = verified_admin()
    page = client.get("/admin/plants/?issue=blocked_provider_credentials")
    assert [plant.id for plant in page.context["plants"]] == [system.region.pk]
    assert page.context["health_filter"].key == "blocked_provider_credentials"
    assert 'name="issue" value="blocked_provider_credentials"' in page.content.decode()
    assert "issue=blocked_provider_credentials" in page.context["pagination_query"]
    assert (
        plant_list_context("no matching plant", "", "1", "blocked_provider_credentials")["plants"]
        == []
    )
    assert plant_list_context("", "", "1", "unknown")["health_filter"] is None
    record_dispatch_phase("notifications", NOW, "OperationalError")
    view = operations_context("dispatch")
    assert view["show_dispatch"] is True
    assert view["show_collections"] is False


def test_metering_periods_publication_waits_and_email_content_are_distinct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(timezone, "now", lambda: NOW)
    system, _, _ = connected(monkeypatch)
    job = EpiasCollectionJob.objects.first()
    assert job is not None
    job.source_kind = "settlement_injection"
    job.period_end_day = job.target_day + timedelta(days=29)
    job.status = "awaiting_publication"
    job.last_error = "unpublished_data"
    job.scheduled_for = NOW + timedelta(seconds=1)
    job.save()
    context = recent_work_context(region_id=system.region.pk, limit=50)
    row = next(row for row in context["metering"] if row["id"] == job.pk)
    assert row["status_label"] == "Waiting for provider publication"
    assert row["source_label"] == "Settlement production"
    assert row["period_end_day"] == job.period_end_day
    assert row["error_label"] == ""
    job.refresh_from_db()
    assert job.last_error == "unpublished_data"
    schedule = ReminderSchedule.objects.create(region=system.region, effective_from=NOW)
    NotificationDelivery.objects.create(
        schedule=schedule,
        binding=system.binding,
        scheduled_for=NOW,
        preparation_deadline=NOW + PREPARATION_GRACE,
        available_at=NOW,
        recipients=["recipient@example.test"],
        prepared_message={"kind": "unavailable"},
    )
    assert recent_work_context()["deliveries"][0]["forecast_status"] == "Forecast unavailable"
    client, _ = verified_admin()
    response = client.get("/admin/operations/")
    assert response.status_code == 200
    assert "Waiting for provider publication" in response.content.decode()
