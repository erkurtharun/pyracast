from typing import ClassVar

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.migrations.operations.base import Operation

REASONS = [
    ("cancelled", "Cancelled"),
    ("superseded", "Superseded"),
    ("member_removed", "Member removed"),
    ("administrator_changed", "Administrator changed"),
]


class Migration(migrations.Migration):
    """Every row is kept. A revocation before this migration stored no reason and an acceptance
    no account, so existing rows keep an empty reason and no acceptor."""

    dependencies: ClassVar = [
        ("accounts", "0007_invitation_token_minted_at_send"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations: ClassVar[list[Operation]] = [
        migrations.AddField(
            model_name="workspaceinvitation",
            name="revoked_reason",
            field=models.CharField(blank=True, choices=REASONS, default="", max_length=21),
        ),
        migrations.AddField(
            model_name="workspaceinvitation",
            name="accepted_by",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.RemoveConstraint(
            model_name="workspaceinvitation",
            name="workspaceinvitation_revoked_at_chk",
        ),
        migrations.AddConstraint(
            model_name="workspaceinvitation",
            constraint=models.CheckConstraint(
                condition=models.Q(("revoked_reason__in", ["", *(value for value, _ in REASONS)])),
                name="workspaceinvitation_revoked_reason_chk",
            ),
        ),
        migrations.AddConstraint(
            model_name="workspaceinvitation",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("revoked_at__isnull", True), ("revoked_reason", "")),
                    models.Q(("accepted_at__isnull", True), ("revoked_at__isnull", False)),
                    _connector="OR",
                ),
                name="workspaceinvitation_revoked_at_chk",
            ),
        ),
        migrations.AddConstraint(
            model_name="workspaceinvitation",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("accepted_by__isnull", True), ("accepted_at__isnull", False), _connector="OR"
                ),
                name="workspaceinvitation_accepted_by_chk",
            ),
        ),
    ]
