from django.db import migrations


class Migration(migrations.Migration):
    dependencies = (("notifications", "0010_notificationdelivery_message_token"),)

    operations = (
        migrations.RunSQL(
            "UPDATE notifications_notificationdelivery SET last_error = 'mail_credentials_rejected'"
            " WHERE last_error = 'mail_authentication_failed'",
            migrations.RunSQL.noop,
        ),
    )
