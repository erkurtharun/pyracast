from datetime import datetime
from typing import ClassVar

from django.apps.registry import Apps
from django.conf import settings
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.operations.base import Operation

from pyracast.db import fire_deferred_checks, refuse_rows

ADMIN = "admin"


def close_superseded_invitations(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    """The previous writers ended an invitation by moving its deadline, at the latest when a newer
    invitation of its address, or of the administrator role, was created. Such an invitation is
    revoked at its deadline; one still open past that creation was stored by no writer and is
    refused. No other row is written."""
    invitations = apps.get_model("accounts", "WorkspaceInvitation")._default_manager.using(
        schema_editor.connection.alias
    )
    unaccepted = (
        invitations.filter(accepted_at__isnull=True)
        .order_by("-tenant_id", "-pk")
        .values("pk", "tenant_id", "email", "role", "created_at", "expires_at")
    )
    earliest_address: dict[tuple[int, str], datetime] = {}
    earliest_administrator: dict[int, datetime] = {}
    ended: dict[int, datetime] = {}
    open_after_successor: list[int] = []
    for row in unaccepted.iterator():
        address = (row["tenant_id"], row["email"])
        successors = [
            created
            for created in (
                earliest_address.get(address),
                earliest_administrator.get(row["tenant_id"]) if row["role"] == ADMIN else None,
            )
            if created is not None
        ]
        if successors and row["expires_at"] <= min(successors):
            ended[row["pk"]] = row["expires_at"]
        elif successors:
            open_after_successor.append(row["pk"])
        earliest_address[address] = min(
            earliest_address.get(address, row["created_at"]), row["created_at"]
        )
        if row["role"] == ADMIN:
            earliest_administrator[row["tenant_id"]] = min(
                earliest_administrator.get(row["tenant_id"], row["created_at"]), row["created_at"]
            )
    if open_after_successor:
        open_after_successor.reverse()
        raise ValueError(
            f"accounts_workspaceinvitation holds {len(open_after_successor)} row(s) still open "
            "when a newer invitation of their address, or of the administrator role, was "
            f"created: ids {open_after_successor}. Correct these rows and migrate again; nothing "
            "was changed."
        )
    for key, deadline in ended.items():
        invitations.filter(pk=key).update(revoked_at=deadline)
    fire_deferred_checks(schema_editor)


def unchanged_administrator(model: type[models.Model]) -> models.Q:
    return models.Q(administrator=models.F("previous_administrator"))


class Migration(migrations.Migration):
    dependencies: ClassVar = [
        ("accounts", "0003_workspace_invitation_role"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations: ClassVar[list[Operation]] = [
        migrations.AddField(
            model_name="workspaceinvitation",
            name="revoked_at",
            field=models.DateTimeField(null=True),
        ),
        migrations.RunPython(close_superseded_invitations, migrations.RunPython.noop),
        refuse_rows(
            "accounts.workspaceadminchange",
            unchanged_administrator,
            "an administrator change names its administrator and a different previous one",
        ),
        migrations.AddConstraint(
            model_name="workspaceadminchange",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("administrator", models.F("previous_administrator")), _negated=True
                ),
                name="workspaceadminchange_administrator_chk",
            ),
        ),
        migrations.AddConstraint(
            model_name="workspaceinvitation",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("revoked_at__isnull", True),
                    models.Q(
                        ("accepted_at__isnull", True),
                        ("expires_at__lte", models.F("revoked_at")),
                    ),
                    _connector="OR",
                ),
                name="workspaceinvitation_revoked_at_chk",
            ),
        ),
        migrations.AddConstraint(
            model_name="workspaceinvitation",
            constraint=models.UniqueConstraint(
                condition=models.Q(("accepted_at__isnull", True), ("revoked_at__isnull", True)),
                fields=("tenant", "email"),
                name="workspaceinvitation_tenant_email_uniq",
            ),
        ),
        migrations.AddConstraint(
            model_name="workspaceinvitation",
            constraint=models.UniqueConstraint(
                condition=models.Q(
                    ("accepted_at__isnull", True), ("revoked_at__isnull", True), ("role", ADMIN)
                ),
                fields=("tenant",),
                name="workspaceinvitation_tenant_administrator_uniq",
            ),
        ),
    ]
