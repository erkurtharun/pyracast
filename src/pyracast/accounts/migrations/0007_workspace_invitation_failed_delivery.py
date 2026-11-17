from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = (("accounts", "0006_workspace_member_removal"),)

    operations = (
        migrations.AlterField(
            model_name="workspaceinvitation",
            name="delivery_status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending"),
                    ("accepted", "Accepted"),
                    ("failed", "Failed"),
                    ("uncertain", "Uncertain"),
                ],
                default="pending",
                max_length=9,
            ),
        ),
    )
