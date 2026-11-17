from collections.abc import Iterator
from typing import Any, cast

import pytest
from django.apps.registry import Apps
from django.contrib.auth.models import User
from django.db import IntegrityError, connection, transaction
from django.db.migrations.exceptions import IrreversibleError
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.recorder import MigrationRecorder
from django.utils import timezone

from pyracast.accounts.models import Membership, Tenant
from pyracast.notifications.models import ReminderSchedule
from pyracast.regions.models import Region

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def legacy_reminders() -> Iterator[Apps]:
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
    before = [node for node in latest if node[0] != "notifications"] + [
        ("notifications", "0007_notificationdelivery_language")
    ]
    executor.migrate(before)
    try:
        yield executor.loader.project_state(before).apps
    finally:
        MigrationExecutor(connection).migrate(latest)


def test_shared_reminder_migration_preserves_schedules_and_frozen_delivery_history(
    legacy_reminders: Apps,
) -> None:
    old_user = legacy_reminders.get_model("auth", "User")
    old_tenant = legacy_reminders.get_model("accounts", "Tenant")
    old_member = legacy_reminders.get_model("accounts", "Membership")
    old_language = legacy_reminders.get_model("accounts", "UserLanguagePreference")
    old_region = legacy_reminders.get_model("regions", "Region")
    old_preference = cast(Any, legacy_reminders).get_model("notifications", "ReminderPreference")
    old_delivery = cast(Any, legacy_reminders).get_model("notifications", "NotificationDelivery")
    tenant = old_tenant.objects.create(name="Reminder migration")
    administrator = old_user.objects.create(
        username="admin", email="old-admin@example.com", is_active=True
    )
    viewer = old_user.objects.create(username="viewer", email="viewer@example.com", is_active=True)
    inactive = old_user.objects.create(
        username="inactive", email="inactive@example.com", is_active=False
    )
    invalid = old_user.objects.create(username="invalid", email="", is_active=True)
    invalid_format = old_user.objects.create(
        username="invalid-format", email="invalid-address", is_active=True
    )
    departed = old_user.objects.create(
        username="departed", email="departed@example.com", is_active=True
    )
    old_member.objects.create(tenant=tenant, user=administrator, role="admin")
    for user in (viewer, invalid, invalid_format):
        old_member.objects.create(tenant=tenant, user=user, role="viewer")
    old_language.objects.create(user=administrator, language="tr")
    regions = [
        old_region.objects.create(
            tenant=tenant,
            name=f"Region {number}",
            latitude=float(number),
            longitude=1.0,
            timezone="Europe/Istanbul",
        )
        for number in range(7)
    ]
    now = timezone.now()
    viewer_setting = old_preference.objects.create(
        user=viewer,
        region=regions[0],
        enabled=True,
        recipients=["viewer-only@example.com"],
        times=["09:00"],
        effective_from=now,
    )
    admin_setting = old_preference.objects.create(
        user=administrator,
        region=regions[0],
        enabled=False,
        recipients=[],
        times=[],
        effective_from=now,
    )
    inactive_setting = old_preference.objects.create(
        user=inactive,
        region=regions[1],
        enabled=True,
        recipients=[],
        times=["07:00"],
        effective_from=now,
    )
    fallback_setting = old_preference.objects.create(
        user=viewer,
        region=regions[1],
        enabled=True,
        recipients=[],
        times=["08:00"],
        effective_from=now,
    )
    inactive_only = old_preference.objects.create(
        user=inactive,
        region=regions[2],
        enabled=True,
        recipients=["preserved@example.com"],
        times=["10:00"],
        effective_from=now,
    )
    invalid_setting = old_preference.objects.create(
        user=invalid,
        region=regions[3],
        enabled=True,
        recipients=[],
        times=["11:00"],
        effective_from=now,
    )
    malformed_setting = old_preference.objects.create(
        user=invalid_format,
        region=regions[4],
        enabled=True,
        recipients=[],
        times=["12:00"],
        effective_from=now,
    )
    departed_setting = old_preference.objects.create(
        user=departed,
        region=regions[5],
        enabled=True,
        recipients=[],
        times=["13:00"],
        effective_from=now,
    )
    for preference, status, recipient, language in (
        (viewer_setting, "sent", "pinned-viewer@example.com", "en"),
        (admin_setting, "uncertain", "pinned-admin@example.com", "tr"),
    ):
        old_delivery.objects.create(
            preference=preference,
            status=status,
            recipient=recipient,
            language=language,
            scheduled_for=now,
            available_at=now,
            prepared_message={"text": "Frozen content"},
            sent_at=now if status == "sent" else None,
            transmit_started_at=now,
            attempts=2,
        )
    original_preferences = set(old_preference.objects.values_list("pk", flat=True))
    original_deliveries = list(old_delivery.objects.order_by("pk").values())
    executor = MigrationExecutor(connection)
    grouped = [
        node for node in executor.loader.graph.leaf_nodes() if node[0] != "notifications"
    ] + [("notifications", "0009_grouped_reminders")]
    executor.migrate(grouped)
    migrated_deliveries = cast(Any, executor.loader.project_state(grouped).apps).get_model(
        "notifications", "NotificationDelivery"
    )
    assert connection.vendor == "postgresql"
    assert set(ReminderSchedule.objects.values_list("pk", flat=True)) == original_preferences
    chosen = ReminderSchedule.objects.get(pk=admin_setting.pk)
    assert chosen.retired_at is None
    assert not chosen.enabled
    assert chosen.recipients == ["old-admin@example.com"]
    assert chosen.language == "tr"
    assert chosen.times == []
    assert chosen.effective_from == now
    assert chosen.configured_by_id == administrator.pk
    retired = ReminderSchedule.objects.get(pk=viewer_setting.pk)
    assert retired.retired_at is not None
    assert not retired.enabled
    assert retired.recipients == ["viewer-only@example.com"]
    assert retired.times == ["09:00"]
    assert retired.language == "en"
    fallback = ReminderSchedule.objects.get(pk=fallback_setting.pk)
    assert fallback.retired_at is None
    assert fallback.enabled
    assert fallback.recipients == ["viewer@example.com"]
    retired_inactive = ReminderSchedule.objects.get(pk=inactive_setting.pk)
    assert retired_inactive.retired_at is not None
    assert retired_inactive.recipients == ["inactive@example.com"]
    inactive_schedule = ReminderSchedule.objects.get(pk=inactive_only.pk)
    assert inactive_schedule.retired_at is None
    assert not inactive_schedule.enabled
    assert inactive_schedule.recipients == ["preserved@example.com"]
    for pk in (invalid_setting.pk, malformed_setting.pk):
        invalid_schedule = ReminderSchedule.objects.get(pk=pk)
        assert invalid_schedule.retired_at is None
        assert not invalid_schedule.enabled
        assert invalid_schedule.recipients == []
    departed_schedule = ReminderSchedule.objects.get(pk=departed_setting.pk)
    assert departed_schedule.retired_at is None
    assert not departed_schedule.enabled
    assert departed_schedule.recipients == ["departed@example.com"]
    assert not ReminderSchedule.objects.filter(region_id=regions[6].pk).exists()
    for original in original_deliveries:
        expected = original.copy()
        expected["schedule_id"] = expected.pop("preference_id")
        expected["recipients"] = [expected.pop("recipient")]
        expected["recipient_memberships"] = {}
        expected["revoked_recipients"] = []
        expected["grouped"] = False
        assert migrated_deliveries.objects.values().get(pk=original["id"]) == expected
    User.objects.filter(pk=administrator.pk).update(email="replacement@example.com")
    chosen.refresh_from_db()
    assert chosen.recipients == ["old-admin@example.com"]
    User.objects.get(pk=administrator.pk).delete()
    chosen.refresh_from_db()
    assert ReminderSchedule.objects.get(pk=chosen.pk).configured_by_id is None
    assert chosen.recipients == ["old-admin@example.com"]
    assert migrated_deliveries.objects.count() == 2
    with pytest.raises(IntegrityError), transaction.atomic():
        ReminderSchedule.objects.create(region_id=regions[0].pk, effective_from=now)
    with pytest.raises(IntegrityError), transaction.atomic():
        ReminderSchedule.objects.filter(pk=chosen.pk).update(language="fr")
    delivery = migrated_deliveries.objects.first()
    assert delivery is not None
    migrated_deliveries.objects.create(
        schedule_id=delivery.schedule_id,
        scheduled_for=delivery.scheduled_for,
        recipients=delivery.recipients,
        available_at=now,
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        migrated_deliveries.objects.create(
            schedule_id=delivery.schedule_id,
            scheduled_for=delivery.scheduled_for,
            recipients=delivery.recipients,
            available_at=now,
        )


@pytest.fixture
def shared_reminders_schema() -> Iterator[Apps]:
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
    target = [node for node in latest if node[0] != "notifications"] + [
        ("notifications", "0008_shared_reminder_schedules")
    ]
    executor.migrate(target)
    try:
        yield executor.loader.project_state(target).apps
    finally:
        MigrationExecutor(connection).migrate(latest)


@pytest.mark.parametrize("missing_configurer", [True, False])
def test_unsafe_reminder_rollback_keeps_latest_schema_and_data(
    missing_configurer: bool,
    shared_reminders_schema: Apps,
) -> None:
    schedules = cast(Any, shared_reminders_schema).get_model("notifications", "ReminderSchedule")
    deliveries = cast(Any, shared_reminders_schema).get_model(
        "notifications", "NotificationDelivery"
    )
    now = timezone.now()
    tenant = Tenant.objects.create(name="Blocked reminder rollback")
    user = User.objects.create(username="configurer", email="configurer@example.com")
    region = Region.objects.create(
        tenant=tenant, name="Region", latitude=1.0, longitude=1.0, timezone="UTC"
    )
    schedule = schedules.objects.create(
        configured_by_id=None if missing_configurer else user.pk,
        region_id=region.pk,
        recipients=["preserved@example.com"],
        times=["10:00"],
        language="tr",
        effective_from=now,
        enabled=True,
    )
    if not missing_configurer:
        schedules.objects.create(
            configured_by_id=user.pk,
            region_id=region.pk,
            recipients=["archived@example.com"],
            times=["09:00"],
            effective_from=now,
            enabled=False,
            retired_at=now,
        )
    deliveries.objects.create(
        schedule=schedule,
        scheduled_for=now,
        available_at=now,
        recipient="frozen@example.com",
        language="tr",
        status="uncertain",
        prepared_message={"body": "Frozen content"},
        attempts=1,
        transmit_started_at=now,
    )
    original_schedules = list(schedules.objects.order_by("pk").values())
    original_deliveries = list(deliveries.objects.order_by("pk").values())
    executor = MigrationExecutor(connection)
    before = [node for node in executor.loader.graph.leaf_nodes() if node[0] != "notifications"] + [
        ("notifications", "0007_notificationdelivery_language")
    ]
    with pytest.raises(IrreversibleError, match="restore a pre-migration database backup"):
        executor.migrate(before)
    assert list(schedules.objects.order_by("pk").values()) == original_schedules
    assert list(deliveries.objects.order_by("pk").values()) == original_deliveries
    assert ("notifications", "0008_shared_reminder_schedules") in MigrationRecorder(
        connection
    ).applied_migrations()
    with connection.cursor() as cursor:
        tables = connection.introspection.table_names(cursor)
        schedule_constraints = connection.introspection.get_constraints(
            cursor, schedules._meta.db_table
        )
        delivery_constraints = connection.introspection.get_constraints(
            cursor, deliveries._meta.db_table
        )
        fields = connection.introspection.get_table_description(cursor, schedules._meta.db_table)
    assert "notifications_reminderschedule" in tables
    assert "notifications_reminderpreference" not in tables
    assert {
        "one_current_region_reminder_schedule",
        "reminder_schedule_language_valid",
    } <= schedule_constraints.keys()
    assert "unique_reminder_recipient_slot" in delivery_constraints
    assert next(field for field in fields if field.name == "configured_by_id").null_ok
    with pytest.raises(IntegrityError), transaction.atomic():
        schedules.objects.create(region_id=region.pk, effective_from=now)


def test_compatible_reminder_rollback_preserves_all_rows_and_never_reenables_archives() -> None:
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
    grouped = [node for node in latest if node[0] != "notifications"] + [
        ("notifications", "0009_grouped_reminders")
    ]
    executor.migrate(grouped)
    deliveries = cast(Any, executor.loader.project_state(grouped).apps).get_model(
        "notifications", "NotificationDelivery"
    )
    now = timezone.now()
    tenant = Tenant.objects.create(name="Compatible reminder rollback")
    administrator = User.objects.create(username="admin", email="admin@example.com")
    former = User.objects.create(username="former", email="former@example.com")
    Membership.objects.create(tenant=tenant, user=administrator, role="admin")
    Membership.objects.create(tenant=tenant, user=former, role="viewer")
    region = Region.objects.create(
        tenant=tenant, name="Region", latitude=1.0, longitude=1.0, timezone="UTC"
    )
    current = ReminderSchedule.objects.create(
        configured_by=administrator,
        region=region,
        recipients=["current@example.com"],
        times=["10:00"],
        effective_from=now,
        enabled=True,
    )
    archived = ReminderSchedule.objects.create(
        configured_by=former,
        region=region,
        recipients=["archived@example.com"],
        times=["09:00"],
        effective_from=now,
        enabled=False,
        retired_at=now,
    )
    for schedule in (current, archived):
        deliveries.objects.create(
            schedule_id=schedule.pk,
            scheduled_for=now,
            available_at=now,
            recipients=list(schedule.recipients),
            grouped=False,
            status="sent",
            language="tr",
            prepared_message={"body": "Frozen content"},
            attempts=1,
            transmit_started_at=now,
            sent_at=now,
        )
    original_deliveries = list(deliveries.objects.order_by("pk").values())
    original_preferences = [
        {
            "id": schedule.pk,
            "user_id": schedule.configured_by_id,
            "region_id": region.pk,
            "enabled": schedule.enabled,
            "recipients": schedule.recipients,
            "times": schedule.times,
            "effective_from": schedule.effective_from,
        }
        for schedule in (current, archived)
    ]
    before = [node for node in latest if node[0] != "notifications"] + [
        ("notifications", "0007_notificationdelivery_language")
    ]
    try:
        executor = MigrationExecutor(connection)
        executor.migrate(before)
        historical = executor.loader.project_state(before).apps
        preferences = cast(Any, historical).get_model("notifications", "ReminderPreference")
        legacy_deliveries = cast(Any, historical).get_model("notifications", "NotificationDelivery")
        assert list(preferences.objects.order_by("pk").values()) == original_preferences
        for original in original_deliveries:
            expected: dict[str, object] = dict(original)
            expected["preference_id"] = expected.pop("schedule_id")
            addresses = cast(list[str], expected.pop("recipients"))
            expected["recipient"] = addresses[0]
            expected.pop("recipient_memberships")
            assert expected.pop("revoked_recipients") == []
            expected.pop("grouped")
            assert legacy_deliveries.objects.values().get(pk=original["id"]) == expected
    finally:
        MigrationExecutor(connection).migrate(latest)
    assert ReminderSchedule.objects.count() == 2
    assert ReminderSchedule.objects.get(pk=current.pk).retired_at is None
    assert ReminderSchedule.objects.get(pk=current.pk).enabled
    assert ReminderSchedule.objects.get(pk=archived.pk).retired_at is not None
    assert not ReminderSchedule.objects.get(pk=archived.pk).enabled
    assert list(deliveries.objects.order_by("pk").values()) == original_deliveries
