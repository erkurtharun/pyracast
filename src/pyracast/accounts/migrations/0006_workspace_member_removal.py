import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = (
        ("accounts", "0005_workspaceadminchange_workspaceinvitation_and_more"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    )

    operations = (
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
    )
