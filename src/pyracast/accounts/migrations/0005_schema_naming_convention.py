from typing import ClassVar

from django.db import migrations, models
from django.db.migrations.operations.base import Operation

from pyracast.db import RenameInPlace


class Migration(migrations.Migration):
    dependencies: ClassVar = [("accounts", "0004_invitation_and_administrator_change_checks")]

    operations: ClassVar[list[Operation]] = [
        RenameInPlace(
            model_name="tenant",
            old_name="workspace_name_not_blank",
            item=models.CheckConstraint(
                condition=models.Q(("name__regex", "^\\s*$"), _negated=True), name="tenant_name_chk"
            ),
        ),
        RenameInPlace(
            model_name="membership",
            old_name="unique_tenant_membership",
            item=models.UniqueConstraint(
                fields=("tenant", "user"), name="membership_tenant_user_uniq"
            ),
        ),
        RenameInPlace(
            model_name="membership",
            old_name="one_workspace_admin",
            item=models.UniqueConstraint(
                condition=models.Q(("role", "admin")),
                fields=("tenant",),
                name="membership_tenant_administrator_uniq",
            ),
        ),
        RenameInPlace(
            model_name="membership",
            old_name="valid_workspace_role",
            item=models.CheckConstraint(
                condition=models.Q(("role__in", ["admin", "viewer"])), name="membership_role_chk"
            ),
        ),
        RenameInPlace(
            model_name="userlanguagepreference",
            old_name="supported_user_language",
            item=models.CheckConstraint(
                condition=models.Q(("language__in", ["en", "tr"])),
                name="userlanguagepreference_language_chk",
            ),
        ),
    ]
