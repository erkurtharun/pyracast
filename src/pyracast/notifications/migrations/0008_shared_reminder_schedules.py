from typing import Any, cast

from django.apps.registry import Apps
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, validate_email
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.exceptions import IrreversibleError
from django.utils import timezone


def preserve_reminder_schedules(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    schedule_model = cast(Any, apps).get_model("notifications", "ReminderSchedule")
    membership_model = apps.get_model("accounts", "Membership")
    language_model = apps.get_model("accounts", "UserLanguagePreference")
    alias = schema_editor.connection.alias
    now = timezone.now()
    region_ids = schedule_model.objects.using(alias).values_list("region_id", flat=True).distinct()
    for region_id in region_ids:
        schedules = list(
            schedule_model.objects.using(alias)
            .filter(region_id=region_id)
            .select_related("configured_by", "region")
            .order_by("pk")
        )
        admin_id = (
            membership_model.objects.using(alias)
            .filter(tenant_id=schedules[0].region.tenant_id, role="admin", user__is_active=True)
            .values_list("user_id", flat=True)
            .first()
        )
        eligible_user_ids = set(
            membership_model.objects.using(alias)
            .filter(tenant_id=schedules[0].region.tenant_id, user__is_active=True)
            .values_list("user_id", flat=True)
        )
        selected = next(
            (schedule for schedule in schedules if schedule.configured_by_id == admin_id), None
        )
        if selected is None:
            selected = next(
                (
                    schedule
                    for schedule in schedules
                    if schedule.configured_by_id in eligible_user_ids
                ),
                schedules[0],
            )
        for schedule in schedules:
            language = (
                language_model.objects.using(alias)
                .filter(user_id=schedule.configured_by_id)
                .values_list("language", flat=True)
                .first()
            ) or "en"
            updates: dict[str, object] = {"language": language}
            if schedule.configured_by_id not in eligible_user_ids:
                updates["enabled"] = False
            if not schedule.recipients:
                email = schedule.configured_by.email
                try:
                    validate_email(email)
                except ValidationError:
                    updates["enabled"] = False
                else:
                    updates["recipients"] = [email]
            if schedule.pk != selected.pk:
                updates.update(enabled=False, retired_at=now)
            schedule_model.objects.using(alias).filter(pk=schedule.pk).update(**updates)


def require_legacy_schedule_ownership(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    schedule_model = cast(Any, apps).get_model("notifications", "ReminderSchedule")
    schedules = schedule_model.objects.using(schema_editor.connection.alias)
    if schedules.filter(configured_by_id__isnull=True).exists():
        raise IrreversibleError(
            "Reminder schedules without a configuring user cannot become legacy preferences. "
            "To roll back, restore a pre-migration database backup."
        )
    if (
        schedules.values("configured_by_id", "region_id")
        .annotate(total=models.Count("pk"))
        .filter(total__gt=1)
        .exists()
    ):
        raise IrreversibleError(
            "Reminder schedules share a configuring user and region, which legacy preferences "
            "cannot represent. To roll back, restore a pre-migration database backup."
        )


class Migration(migrations.Migration):
    dependencies = (
        ("accounts", "0005_workspaceadminchange_workspaceinvitation_and_more"),
        ("notifications", "0007_notificationdelivery_language"),
    )

    operations = (
        migrations.RemoveConstraint(
            model_name="reminderpreference", name="unique_user_region_reminder"
        ),
        migrations.RemoveConstraint(
            model_name="notificationdelivery", name="unique_reminder_recipient_slot"
        ),
        migrations.RenameModel(old_name="ReminderPreference", new_name="ReminderSchedule"),
        migrations.RenameField(
            model_name="reminderschedule", old_name="user", new_name="configured_by"
        ),
        migrations.AlterField(
            model_name="reminderschedule",
            name="configured_by",
            field=models.ForeignKey(
                null=True, on_delete=models.SET_NULL, to=settings.AUTH_USER_MODEL
            ),
        ),
        migrations.AddField(
            model_name="reminderschedule",
            name="language",
            field=models.CharField(
                choices=[("en", "English"), ("tr", "Türkçe")], default="en", max_length=2
            ),
        ),
        migrations.AddField(
            model_name="reminderschedule", name="retired_at", field=models.DateTimeField(null=True)
        ),
        migrations.RenameField(
            model_name="notificationdelivery", old_name="preference", new_name="schedule"
        ),
        migrations.RunPython(preserve_reminder_schedules, require_legacy_schedule_ownership),
        migrations.AddConstraint(
            model_name="reminderschedule",
            constraint=models.UniqueConstraint(
                fields=("region",),
                condition=models.Q(retired_at__isnull=True),
                name="one_current_region_reminder_schedule",
            ),
        ),
        migrations.AddConstraint(
            model_name="reminderschedule",
            constraint=models.CheckConstraint(
                condition=models.Q(language__in=("en", "tr")),
                name="reminder_schedule_language_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="notificationdelivery",
            constraint=models.UniqueConstraint(
                fields=("schedule", "scheduled_for", "recipient"),
                name="unique_reminder_recipient_slot",
            ),
        ),
        migrations.AlterField(
            model_name="notificationsettings",
            name="max_recipients",
            field=models.PositiveSmallIntegerField(
                default=5,
                validators=[MinValueValidator(1)],
                help_text=(
                    "Global recipient limit per plant reminder schedule. Lowering it pauses "
                    "reminders at plants exceeding the limit until their recipient lists "
                    "are revised."
                ),
            ),
        ),
    )
