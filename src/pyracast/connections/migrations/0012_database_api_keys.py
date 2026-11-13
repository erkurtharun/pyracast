from typing import ClassVar

from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.state import StateApps

ENCRYPTED_PREFIX = "encrypted:v1:"


def prepare_database_keys(apps: StateApps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    accounts = apps.get_model("connections", "ProviderAccount").objects.using(
        schema_editor.connection.alias
    )
    if (
        accounts.exclude(provider="fixture")
        .exclude(secret_reference__startswith=ENCRYPTED_PREFIX)
        .exists()
    ):
        raise RuntimeError(
            "Transfer every Solcast API key to encrypted database storage before this migration."
        )
    accounts.filter(provider="fixture").exclude(
        secret_reference__startswith=ENCRYPTED_PREFIX
    ).update(secret_reference="")


class Migration(migrations.Migration):
    dependencies: ClassVar = [("connections", "0011_canonical_account_identity")]

    operations: ClassVar = [
        migrations.RunPython(prepare_database_keys, migrations.RunPython.noop),
        migrations.RenameField("provideraccount", "secret_reference", "encrypted_api_key"),
        migrations.AlterField(
            model_name="provideraccount",
            name="encrypted_api_key",
            field=models.CharField(blank=True, default="", max_length=2048),
        ),
        migrations.AddConstraint(
            model_name="provideraccount",
            constraint=models.CheckConstraint(
                condition=models.Q(provider="fixture", encrypted_api_key="")
                | models.Q(encrypted_api_key__startswith="encrypted:v1:"),
                name="provider_account_encrypted_api_key",
            ),
        ),
    ]
