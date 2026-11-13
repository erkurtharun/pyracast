from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = (("regions", "0002_account_audit_targets"),)

    operations = (
        migrations.AddField(
            model_name="region",
            name="deleted_at",
            field=models.DateTimeField(db_index=True, null=True),
        ),
    )
