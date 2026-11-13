from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = (("connections", "0012_database_api_keys"),)

    operations = (
        migrations.AddField(
            model_name="provideraccount",
            name="authentication_failed_revision",
            field=models.PositiveIntegerField(null=True),
        ),
        migrations.AddField(
            model_name="provideraccount",
            name="request_blocked_until",
            field=models.DateTimeField(null=True),
        ),
    )
