import sys
from typing import ClassVar

from django.apps.registry import Apps
from django.core.exceptions import ImproperlyConfigured
from django.core.management.base import CommandError
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.operations.base import Operation

from pyracast.connections.contracts import ProviderError
from pyracast.connections.secrets import (
    FINGERPRINT_CONTEXT,
    api_key_fingerprints,
    decrypt_api_key,
)
from pyracast.encryption import derived_keys


def _require_application_key() -> None:
    try:
        derived_keys(FINGERPRINT_CONTEXT)
    except ImproperlyConfigured:
        raise CommandError(
            "connections.0005 needs PYRACAST_SECRET_KEY to fingerprint the stored provider API "
            "keys; nothing was changed."
        ) from None


def fill_fingerprints(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    """The first account holding a key gets its fingerprint. A later account with the same key,
    or one whose key no configured application key can read, keeps it empty and is listed by
    id; replacing its key fills it."""
    _require_application_key()
    accounts = apps.get_model("connections", "ProviderAccount")._default_manager.using(
        schema_editor.connection.alias
    )
    holders: set[str] = set()
    shared: list[int] = []
    unreadable: list[int] = []
    for pk, encrypted in (
        accounts.exclude(encrypted_api_key="")
        .order_by("pk")
        .values_list("pk", "encrypted_api_key")
        .iterator()
    ):
        try:
            fingerprint = api_key_fingerprints(decrypt_api_key(encrypted))[0]
        except ProviderError:
            unreadable.append(pk)
            continue
        if fingerprint in holders:
            shared.append(pk)
            continue
        holders.add(fingerprint)
        accounts.filter(pk=pk).update(api_key_fingerprint=fingerprint)
    if shared:
        sys.stdout.write(
            f"\n  connections: provider account(s) {', '.join(map(str, shared))} share an API key "
            "with an earlier account; replace their key on the provider account page."
        )
    if unreadable:
        sys.stdout.write(
            f"\n  connections: provider account(s) {', '.join(map(str, unreadable))} hold a key "
            "no configured application key can read; replace their key on the provider account "
            "page."
        )


class Migration(migrations.Migration):
    dependencies: ClassVar = [("connections", "0004_schema_naming_convention")]

    operations: ClassVar[list[Operation]] = [
        migrations.AddField(
            model_name="provideraccount",
            name="api_key_fingerprint",
            field=models.CharField(editable=False, max_length=64, null=True),
        ),
        migrations.AddField(
            model_name="provideraccount",
            name="revalidation_due_at",
            field=models.DateTimeField(null=True),
        ),
        migrations.RunPython(fill_fingerprints, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="provideraccount",
            constraint=models.UniqueConstraint(
                fields=("api_key_fingerprint",), name="provideraccount_api_key_fingerprint_uniq"
            ),
        ),
    ]
