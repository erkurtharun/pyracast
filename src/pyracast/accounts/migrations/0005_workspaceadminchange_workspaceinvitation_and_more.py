import django.db.models.deletion
import django.db.models.functions.text
from django.apps.registry import Apps
from django.conf import settings
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor


def prepare_workspaces(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    tenant_model = apps.get_model("accounts", "Tenant")
    membership_model = apps.get_model("accounts", "Membership")
    alias = schema_editor.connection.alias
    tenants = list(
        tenant_model.objects.using(alias)
        .annotate(
            identity=django.db.models.functions.text.Lower(
                django.db.models.functions.text.Trim("name")
            )
        )
        .order_by("pk")
    )
    reserved = {tenant.identity for tenant in tenants}
    used: set[str] = set()
    for tenant in tenants:
        name = tenant.name
        identity = tenant.identity
        if not name.strip() or identity in used:
            base = name.strip() or "Workspace"
            number = 0
            while True:
                suffix = f" ({tenant.pk})" if not number else f" ({tenant.pk}-{number})"
                candidate = base[: 200 - len(suffix)] + suffix
                candidate_identity = (
                    tenant_model.objects.using(alias)
                    .annotate(
                        candidate_identity=django.db.models.functions.text.Lower(
                            models.Value(candidate)
                        )
                    )
                    .values_list("candidate_identity", flat=True)
                    .first()
                )
                if candidate_identity not in reserved:
                    break
                number += 1
            name = candidate
            identity = candidate_identity
            reserved.add(identity)
        used.add(identity)
        if name != tenant.name:
            tenant_model.objects.using(alias).filter(pk=tenant.pk).update(name=name)
        first = (
            membership_model.objects.using(alias)
            .filter(tenant_id=tenant.pk, user__is_active=True)
            .order_by("pk")
            .first()
        )
        if first is not None:
            membership_model.objects.using(alias).filter(pk=first.pk).update(role="admin")


class Migration(migrations.Migration):
    dependencies = (
        ("accounts", "0004_userlanguagepreference"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    )

    operations = (
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
            ],
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
                            ("accepted", "Accepted"),
                            ("uncertain", "Uncertain"),
                        ],
                        default="pending",
                        max_length=9,
                    ),
                ),
            ],
        ),
        migrations.AddField(
            model_name="membership",
            name="role",
            field=models.CharField(
                choices=[("admin", "Administrator"), ("viewer", "Viewer")],
                default="viewer",
                max_length=6,
            ),
        ),
        migrations.RunPython(prepare_workspaces, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="membership",
            constraint=models.UniqueConstraint(
                condition=models.Q(("role", "admin")),
                fields=("tenant",),
                name="one_workspace_admin",
            ),
        ),
        migrations.AddConstraint(
            model_name="membership",
            constraint=models.CheckConstraint(
                condition=models.Q(("role__in", ("admin", "viewer"))), name="valid_workspace_role"
            ),
        ),
        migrations.AddConstraint(
            model_name="tenant",
            constraint=models.UniqueConstraint(
                django.db.models.functions.text.Lower(django.db.models.functions.text.Trim("name")),
                name="unique_workspace_name",
            ),
        ),
        migrations.AddConstraint(
            model_name="tenant",
            constraint=models.CheckConstraint(
                condition=models.Q(("name__regex", "^\\s*$"), _negated=True),
                name="workspace_name_not_blank",
            ),
        ),
        migrations.AddField(
            model_name="workspaceadminchange",
            name="actor",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="workspace_admin_changes",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="workspaceadminchange",
            name="administrator",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="workspace_admin_assignments",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="workspaceadminchange",
            name="previous_administrator",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="previous_workspace_admin_assignments",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="workspaceadminchange",
            name="tenant",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT, to="accounts.tenant"
            ),
        ),
        migrations.AddField(
            model_name="workspaceinvitation",
            name="created_by",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL
            ),
        ),
        migrations.AddField(
            model_name="workspaceinvitation",
            name="tenant",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE, to="accounts.tenant"
            ),
        ),
    )
