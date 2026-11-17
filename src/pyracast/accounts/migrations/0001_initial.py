from typing import ClassVar

import django.db.models.deletion
import django.db.models.functions.text
from django.conf import settings
from django.db import migrations, models
from django.db.migrations.operations.base import Operation

from pyracast import schema_objects


class Migration(migrations.Migration):
    initial = True

    dependencies: ClassVar = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations: ClassVar[list[Operation]] = [
        migrations.CreateModel(
            name="AuthenticationCacheEntry",
            fields=[
                ("cache_key", models.CharField(max_length=255, primary_key=True, serialize=False)),
                ("value", models.JSONField()),
                ("expires", models.DateTimeField(db_index=True, null=True)),
            ],
            options={
                "db_table": "pyracast_authentication_cache",
            },
        ),
        migrations.CreateModel(
            name="Tenant",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("name", models.CharField(max_length=200)),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        django.db.models.functions.text.Lower(
                            django.db.models.functions.text.Trim("name")
                        ),
                        name="unique_workspace_name",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("name__regex", "^\\s*$"), _negated=True),
                        name="workspace_name_not_blank",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="WorkspaceAdminChange",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "actor",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="workspace_admin_changes",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "administrator",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="workspace_admin_assignments",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "previous_administrator",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="previous_workspace_admin_assignments",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, to="accounts.tenant"
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="WorkspaceMemberRemoval",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "actor",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="workspace_member_removals",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, to="accounts.tenant"
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="workspace_removals",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="Membership",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                (
                    "role",
                    models.CharField(
                        choices=[("admin", "Administrator"), ("viewer", "Viewer")],
                        default="viewer",
                        max_length=6,
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL
                    ),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, to="accounts.tenant"
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("tenant", "user"), name="unique_tenant_membership"
                    ),
                    models.UniqueConstraint(
                        condition=models.Q(("role", "admin")),
                        fields=("tenant",),
                        name="one_workspace_admin",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("role__in", ["admin", "viewer"])),
                        name="valid_workspace_role",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="UserLanguagePreference",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                (
                    "language",
                    models.CharField(choices=[("en", "English"), ("tr", "Türkçe")], max_length=2),
                ),
                (
                    "user",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(("language__in", ["en", "tr"])),
                        name="supported_user_language",
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="WorkspaceInvitation",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("email", models.EmailField(max_length=254)),
                ("token_hash", models.CharField(max_length=64, unique=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("expires_at", models.DateTimeField()),
                ("accepted_at", models.DateTimeField(null=True)),
                (
                    "delivery_status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("sent", "Sent"),
                            ("uncertain", "Uncertain"),
                        ],
                        default="pending",
                        max_length=9,
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL
                    ),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, to="accounts.tenant"
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(
                            ("delivery_status__in", ["pending", "sent", "uncertain"])
                        ),
                        name="workspaceinvitation_delivery_status_chk",
                    )
                ],
            },
        ),
        *schema_objects.operations("accounts", "0001_initial"),
    ]
