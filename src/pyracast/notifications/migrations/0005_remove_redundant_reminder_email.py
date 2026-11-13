from typing import ClassVar

from django.db import migrations


class Migration(migrations.Migration):
    dependencies: ClassVar = [("notifications", "0004_validate_delivery_status")]

    operations: ClassVar = [
        migrations.RunSQL(
            "UPDATE notifications_reminderpreference "
            "SET recipients = jsonb_build_array(email) "
            "WHERE recipients = '[]'::jsonb AND email <> ''",
            "UPDATE notifications_reminderpreference "
            "SET email = CASE WHEN jsonb_array_length(recipients) = 1 "
            "THEN recipients ->> 0 ELSE '' END",
        ),
        migrations.RemoveField(model_name="reminderpreference", name="email"),
    ]
