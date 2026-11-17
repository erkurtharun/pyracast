import uuid

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = (("notifications", "0009_grouped_reminders"),)

    operations = (
        migrations.AddField(
            model_name="notificationdelivery",
            name="message_token",
            field=models.UUIDField(null=True, editable=False),
        ),
        migrations.RunSQL(
            "UPDATE notifications_notificationdelivery SET message_token = gen_random_uuid()",
            migrations.RunSQL.noop,
        ),
        migrations.AlterField(
            model_name="notificationdelivery",
            name="message_token",
            field=models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
        ),
    )
