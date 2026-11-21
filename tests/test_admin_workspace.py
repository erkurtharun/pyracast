from dataclasses import asdict
from datetime import timedelta
from typing import Any

import pytest
from django.contrib.admin.models import LogEntry
from django.contrib.auth.models import User
from django.db import DatabaseError
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from pyracast.accounts.models import Tenant
from pyracast.forecasts.models import DispatcherStatus, RequestAttempt
from pyracast.notifications.models import (
    NotificationDelivery,
    NotificationSettings,
    ReminderSchedule,
)
from pyracast.notifications.operations import set_recipient_limit
from pyracast.notifications.scheduler import PREPARATION_GRACE
from pyracast.plants.models import Plant
from pyracast.platform_admin.read_models import (
    operations_context,
    overview_context,
    plant_list_context,
    recent_work_context,
    review_context,
    summarize_plant,
    workspace_regions,
)
from pyracast.regions.models import AuditEvent, Region
from pyracast.regions.operations import request_location_change
from tests.support.admin_setup import admin_system, setup_input, submitted_plant
from tests.support.api import verified_admin
from tests.support.delivery_health import recent_forecast
from tests.support.domain import make_system
from tests.support.notifications import freeze, ready

pytestmark = pytest.mark.django_db(transaction=True)


def test_delivery_admin_displays_shared_schedule_without_edit_permission() -> None:
    delivery = ready()
    client, _ = verified_admin()
    response = client.get(
        reverse("admin:notifications_notificationdelivery_change", args=(delivery.pk,))
    )
    assert response.status_code == 200
    assert response.context["original"].schedule_id == delivery.schedule_id
    assert response.context["has_change_permission"] is False


def decision_input(region: Region, action: str, **changes: Any) -> dict[str, Any]:
    return {
        "expected_revision": region.revision,
        "expected_status": region.status,
        "action": action,
        **({"decision_reason": "A review reason."} if action in {"reject", "suspend"} else {}),
        **changes,
    }


@pytest.mark.parametrize(
    "identity", ["anonymous", "member", "staff", "unverified_admin", "inactive"]
)
def test_workspace_requires_live_verified_platform_administrator(identity: str) -> None:
    system = make_system()
    client = Client()
    if identity == "inactive":
        client, user = verified_admin()
        user.is_active = False
        user.save(update_fields=("is_active",))
    elif identity != "anonymous":
        user = User.objects.create_user(
            username=f"workspace-{identity}",
            is_staff=identity in {"staff", "unverified_admin"},
            is_superuser=identity == "unverified_admin",
        )
        client.force_login(user)
    for url in (
        "/admin/",
        "/admin/plants/",
        f"/admin/plants/{system.region.pk}/",
        "/admin/operations/",
        "/admin/settings/",
        f"/admin/plants/{system.region.pk}/decision/",
    ):
        assert client.get(url).status_code == 302
        assert client.post(url, {"max_recipients": 50}).status_code == 302
    assert not NotificationSettings.objects.exclude(max_recipients=5).exists()
    assert system.region.status == "pending"


def test_workspace_search_filters_pagination_and_safe_cross_company_summaries() -> None:
    system = admin_system()
    submitted_plant(system)
    other = Tenant.objects.create(name="Different company")
    for index in range(21):
        Region.objects.create(
            tenant=other,
            name=f"Other {index}",
            latitude=index,
            longitude=0,
            timezone="UTC",
            status="suspended",
        )
    Region.objects.create(
        tenant=other,
        name="Deleted",
        latitude=50,
        longitude=0,
        timezone="UTC",
        deleted_at=timezone.now(),
    )
    context = plant_list_context("", "unknown", "2")
    assert context["page_obj"].paginator.count == 22
    assert len(context["plants"]) == 2
    assert context["status"] == ""
    assert len(plant_list_context("Customer solar", "pending", "invalid")["plants"]) == 1
    assert len(plant_list_context("Different", "suspended", "999")["plants"]) == 1
    assert plant_list_context("no-match", "approved", "1")["plants"] == []
    summary = asdict(summarize_plant(workspace_regions().get(pk=system.region.pk)))
    assert summary["plant_name"] == "Customer solar park"
    assert summary["company"] == "Tenant"
    assert "encrypted_api_key" not in str(summary)
    overview = overview_context()
    assert overview["counts"] == {
        "pending": 1,
        "approved": 0,
        "suspended": 21,
        "rejected": 0,
        "location_changes": 0,
        "needs_attention": 0,
    }
    assert len(overview["review_plants"]) == 1


def test_connection_validation_and_saved_data_are_independent_of_approval() -> None:
    system = make_system(approved=True)
    snapshot = recent_forecast(system)
    assert (
        summarize_plant(workspace_regions().get(pk=system.region.pk)).last_collected_at
        == snapshot.collected_at
    )
    assert summarize_plant(workspace_regions().get(pk=system.region.pk)).connection == "Healthy"
    system.account.authentication_failed_revision = system.account.credential_revision
    system.account.save(update_fields=("authentication_failed_revision",))
    assert (
        summarize_plant(workspace_regions().get(pk=system.region.pk)).connection
        == "Needs attention"
    )
    system.account.authentication_failed_revision = None
    system.account.request_blocked_until = timezone.now() + timedelta(hours=1)
    system.account.save(update_fields=("authentication_failed_revision", "request_blocked_until"))
    assert (
        summarize_plant(workspace_regions().get(pk=system.region.pk)).connection == "Provider delay"
    )
    system.account.request_blocked_until = timezone.now() - timedelta(hours=1)
    system.account.save(update_fields=("request_blocked_until",))
    assert summarize_plant(workspace_regions().get(pk=system.region.pk)).connection == "Healthy"
    system.binding.validated_credential_revision = 99
    system.binding.save(update_fields=("validated_credential_revision",))
    assert (
        summarize_plant(workspace_regions().get(pk=system.region.pk)).connection
        == "Needs validation"
    )
    system.binding.validated_at = system.binding.validated_credential_revision = None
    system.binding.save(update_fields=("validated_at", "validated_credential_revision"))
    assert (
        summarize_plant(workspace_regions().get(pk=system.region.pk)).connection
        == "Needs validation"
    )
    system.binding.health = "error"
    system.binding.save(update_fields=("health",))
    assert (
        summarize_plant(workspace_regions().get(pk=system.region.pk)).connection
        == "Needs attention"
    )
    system.binding.health = "healthy"
    system.binding.save(update_fields=("health",))
    system.account.enabled = False
    system.account.save(update_fields=("enabled",))
    assert (
        summarize_plant(workspace_regions().get(pk=system.region.pk)).connection
        == "Needs attention"
    )
    system.account.enabled = True
    system.account.authorized_at = None
    system.account.save(update_fields=("enabled", "authorized_at"))
    assert (
        summarize_plant(workspace_regions().get(pk=system.region.pk)).connection
        == "Needs attention"
    )
    empty = Region.objects.create(
        tenant=system.tenant, name="Empty", latitude=1, longitude=1, timezone="UTC"
    )
    summary = summarize_plant(workspace_regions().get(pk=empty.pk))
    assert summary.connection == "Not configured"
    assert summary.plant_name == "Empty"
    assert summary.last_collected_at is None


def test_workspace_gets_render_without_provider_or_mail_io(monkeypatch: pytest.MonkeyPatch) -> None:
    system = admin_system()
    client, _ = verified_admin()

    def unexpected(*args: object, **kwargs: object) -> None:
        pytest.fail("GET attempted provider or email I/O")

    monkeypatch.setattr("pyracast.platform_admin.plant_review.adapter_for", unexpected)
    monkeypatch.setattr("django.core.mail.send_mail", unexpected)
    for url in (
        "/admin/",
        "/admin/plants/?q=Site&status=pending&page=1",
        f"/admin/plants/{system.region.pk}/",
        "/admin/operations/",
        "/admin/settings/",
    ):
        response = client.get(url)
        assert response.status_code == 200
        assert b"original-fixture-key" not in response.content
        assert system.account.encrypted_api_key.encode() not in response.content
    assert not RequestAttempt.objects.exists()
    assert not NotificationDelivery.objects.exists()


def test_settings_are_validated_audited_and_stale_safe() -> None:
    client, _ = verified_admin()
    url = reverse("admin:workspace_settings")
    NotificationSettings.objects.all().delete()
    assert client.get(url).context["recipient_limit"] == 5
    assert not NotificationSettings.objects.exists()
    csrf = Client(enforce_csrf_checks=True)
    csrf.cookies = client.cookies
    assert csrf.post(url, {"max_recipients": 8, "expected_max_recipients": 5}).status_code == 403
    assert client.post(url, {"max_recipients": 8, "expected_max_recipients": 5}).status_code == 302
    assert NotificationSettings.objects.get().max_recipients == 8
    assert "from 5 to 8" in LogEntry.objects.get().change_message
    for value in (0, -1, 32768, "invalid"):
        assert (
            client.post(url, {"max_recipients": value, "expected_max_recipients": 8}).status_code
            == 400
        )
    assert client.post(url, {"max_recipients": 9, "expected_max_recipients": 5}).status_code == 400
    assert client.post(url, {"max_recipients": 8, "expected_max_recipients": 8}).status_code == 302
    assert LogEntry.objects.count() == 1
    assert NotificationSettings.objects.get().max_recipients == 8
    assert client.put(url, {}).status_code == 405


def test_settings_audit_failure_rolls_back_value(monkeypatch: pytest.MonkeyPatch) -> None:
    system = make_system()
    NotificationSettings.objects.get_or_create(pk=1)

    def fail(*args: object, **kwargs: object) -> None:
        raise DatabaseError("synthetic failure")

    monkeypatch.setattr(LogEntry.objects, "create", fail)
    with pytest.raises(DatabaseError):
        set_recipient_limit(system.admin, 9, expected=5)
    assert NotificationSettings.objects.get().max_recipients == 5


@pytest.mark.parametrize(("action", "status"), [("reject", "rejected"), ("suspend", "suspended")])
def test_review_decisions_are_csrf_and_revision_protected(action: str, status: str) -> None:
    system = make_system(approved=action == "suspend")
    system.region.refresh_from_db()
    initial_status = system.region.status
    client, _ = verified_admin()
    url = reverse("admin:workspace_decision", args=(system.region.pk,))
    assert client.get(url).status_code == 405
    csrf = Client(enforce_csrf_checks=True)
    csrf.cookies = client.cookies
    assert csrf.post(url, decision_input(system.region, action)).status_code == 403
    for data in (
        decision_input(system.region, action, expected_revision=999),
        decision_input(
            system.region,
            action,
            expected_status="approved" if initial_status == "pending" else "pending",
        ),
        {"action": "invalid"},
    ):
        assert client.post(url, data).status_code == 302
        system.region.refresh_from_db()
        assert system.region.status == initial_status
    assert client.post(url, decision_input(system.region, action)).status_code == 302
    system.region.refresh_from_db()
    assert system.region.status == status
    assert AuditEvent.objects.filter(region=system.region, action=f"region_{status}").count() == 1
    system.region.deleted_at = timezone.now()
    system.region.save(update_fields=("deleted_at",))
    assert client.post(url, {}).status_code == 404


@pytest.mark.parametrize("approve", [True, False])
def test_location_review_uses_shared_operation_and_is_scoped_to_the_plant(approve: bool) -> None:
    system = make_system()
    change = request_location_change(
        system.member,
        system.region.pk,
        system.tenant.pk,
        expected_revision=system.region.revision,
        latitude=41,
        longitude=30,
        timezone="UTC",
        reason="Correct pin",
    )
    client, _ = verified_admin()
    context = review_context(system.region.pk)
    assert context["location_changes"][0]["reason"] == "Correct pin"
    assert context["location_changes"][0]["requested_by"] == "member"
    overview = overview_context()
    assert overview["counts"]["location_changes"] == 1
    assert overview["location_change_plants"][0]["review_url"] == reverse(
        "admin:workspace_plant", args=(system.region.pk,)
    )
    url = reverse("admin:workspace_decision", args=(system.region.pk,))
    action = "approve_location" if approve else "reject_location"
    assert (
        client.post(
            url, decision_input(system.region, action, location_change_id=999999)
        ).status_code
        == 302
    )
    change.refresh_from_db()
    assert change.status == "pending"
    assert (
        client.post(
            url, decision_input(system.region, action, location_change_id=change.pk)
        ).status_code
        == 302
    )
    change.refresh_from_db()
    system.region.refresh_from_db()
    assert change.status == ("approved" if approve else "rejected")
    assert system.region.latitude == (41 if approve else 40)
    assert system.region.approved_binding_id is None
    assert review_context(system.region.pk)["location_changes"] == []
    assert (
        "Location change" in review_context(system.region.pk)["recent_activity"][0]["action_label"]
    )


def test_operations_show_safe_recent_records_and_recorded_dispatch_health() -> None:
    system = make_system(approved=True)
    now = timezone.now()
    DispatcherStatus.objects.create(
        phase="notifications",
        attempted_at=now,
        succeeded_at=now - timedelta(minutes=1),
        last_error="",
    )
    schedule = ReminderSchedule.objects.create(
        configured_by=system.member,
        region=system.region,
        enabled=True,
        times=["12:00"],
        effective_from=now,
    )
    delivery = NotificationDelivery.objects.create(
        schedule=schedule,
        binding=system.binding,
        scheduled_for=now,
        preparation_deadline=now + PREPARATION_GRACE,
        available_at=now,
        status="uncertain",
        attempts=1,
    )
    freeze(delivery, ["customer@example.com"])
    context = operations_context()
    assert context["collections"][0]["plant_name"] == "Site"
    assert context["deliveries"][0]["id"] == delivery.pk
    assert context["deliveries"][0]["status"] == "uncertain"
    assert context["deliveries"][0]["recipient"] == "customer@example.com"
    phases = {phase["label"]: phase for phase in context["dispatch_phases"]}
    assert phases["Notifications"]["state_label"] == "Current"
    assert phases["Collection"]["state_label"] == "Needs attention"
    assert context["health"].counts["failed_notifications"] == 1
    assert context["health_summary"].checks
    system.region.deleted_at = now
    system.region.save(update_fields=("deleted_at",))
    assert operations_context()["collections"] == []
    assert operations_context()["deliveries"] == []


def test_current_authorization_is_required_before_the_approval_button_is_enabled() -> None:
    system = admin_system()
    system.binding.validated_at = timezone.now()
    system.binding.validated_credential_revision = system.account.credential_revision
    system.binding.save(update_fields=("validated_at", "validated_credential_revision"))
    client, _ = verified_admin()
    url = reverse("admin:workspace_plant", args=(system.region.pk,))
    assert client.get(url).context["can_approve"] is True
    system.account.authorized_at = None
    system.account.save(update_fields=("authorized_at",))
    assert client.get(url).context["can_approve"] is False
    system.account.authorized_at = timezone.now()
    system.account.save(update_fields=("authorized_at",))
    system.binding.installation_date = None
    system.binding.save(update_fields=("installation_date",))
    assert client.get(url).context["can_approve"] is False


def test_recent_work_uses_the_plant_name_with_a_location_fallback() -> None:
    system = make_system(approved=True)
    now = timezone.now()
    schedule = ReminderSchedule.objects.create(
        configured_by=system.member, region=system.region, effective_from=now
    )
    NotificationDelivery.objects.create(
        schedule=schedule,
        binding=system.binding,
        scheduled_for=now,
        preparation_deadline=now + PREPARATION_GRACE,
        available_at=now,
    )
    context = recent_work_context()
    assert context["collections"][0]["plant_name"] == system.region.name
    assert context["deliveries"][0]["plant_name"] == system.region.name
    Plant.objects.create(region=system.region, name="Named solar plant")
    for context in (recent_work_context(), recent_work_context(system.region.pk, limit=5)):
        assert context["collections"][0]["plant_name"] == "Named solar plant"
        assert context["deliveries"][0]["plant_name"] == "Named solar plant"


def test_workspace_logout_returns_to_shared_sign_in_and_requires_csrf() -> None:
    client, _ = verified_admin()
    url = reverse("admin:logout")
    assert client.get(url).status_code == 405
    csrf = Client(enforce_csrf_checks=True)
    csrf.cookies = client.cookies
    assert csrf.post(url).status_code == 403
    response = client.post(url)
    assert response.status_code == 302
    assert response.headers["Location"] == "/app/"
    assert client.get("/admin/").status_code == 302


@pytest.mark.parametrize("mode", ["existing", "new"])
def test_only_new_provider_accounts_require_a_daily_limit(mode: str) -> None:
    system = admin_system()
    client, _ = verified_admin()
    response = client.post(
        reverse("admin:workspace_plant", args=(system.region.pk,)),
        setup_input(
            system,
            account_mode=mode,
            daily_quota="",
            external_account_id="New account",
            api_key="synthetic-key",
            authorization_reference="Authorized use",
        ),
    )
    assert response.status_code == (302 if mode == "existing" else 400)


def test_overview_keeps_the_oldest_pending_review_visible() -> None:
    system = make_system()
    for index in range(7):
        Region.objects.create(
            tenant=system.tenant,
            name=f"Later plant {index}",
            latitude=index,
            longitude=0,
            timezone="UTC",
        )
    queue = overview_context()["review_plants"]
    assert len(queue) == 6
    assert queue[0].id == system.region.pk
    assert [item.name for item in queue[1:]] == [f"Later plant {index}" for index in range(5)]


@pytest.mark.parametrize(("approved", "action"), [(False, "suspend"), (True, "reject")])
def test_review_rejects_a_decision_outside_the_visible_workflow(
    approved: bool, action: str
) -> None:
    system = make_system(approved=approved)
    system.region.refresh_from_db()
    initial_status = system.region.status
    initial_events = AuditEvent.objects.count()
    client, _ = verified_admin()
    response = client.post(
        reverse("admin:workspace_decision", args=(system.region.pk,)),
        decision_input(system.region, action),
    )
    assert response.status_code == 302
    system.region.refresh_from_db()
    assert system.region.status == initial_status
    assert AuditEvent.objects.count() == initial_events


@pytest.mark.parametrize("value", ["0", "11", "invalid"])
def test_invalid_new_account_quota_has_one_specific_validation_error(value: str) -> None:
    system = admin_system()
    client, _ = verified_admin()
    response = client.post(
        reverse("admin:workspace_plant", args=(system.region.pk,)),
        setup_input(
            system,
            account_mode="new",
            daily_quota=value,
            external_account_id="New account",
            api_key="synthetic-key",
            authorization_reference="Authorized use",
        ),
    )
    assert response.status_code == 400
    assert len(response.context["setup_form"].errors["daily_quota"]) == 1
    assert "required" not in response.context["setup_form"].errors["daily_quota"][0].lower()


def test_approved_plant_with_an_unvalidated_key_has_no_unavailable_approval_instruction() -> None:
    system = make_system(approved=True)
    system.account.credential_revision += 1
    system.account.save(update_fields=("credential_revision",))
    client, _ = verified_admin()
    response = client.get(reverse("admin:workspace_plant", args=(system.region.pk,)))
    assert response.status_code == 200
    assert response.context["can_approve"] is False
    assert b"This plant is approved" in response.content
    assert b"to enable approval" not in response.content
    assert b">Approve plant</button>" not in response.content


def test_console_links_open_the_account_and_customer_workspace_routes() -> None:
    client, _ = verified_admin()
    response = client.get(reverse("admin:workspace_settings"))
    assert response.status_code == 200
    assert response.content.count(b'href="/app/account/"') == 2
    assert response.content.count(b'href="/app/w/"') == 2
    assert b"/app/?" not in response.content
