from typing import ClassVar

from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.operations.base import Operation
from django.db.migrations.state import StateApps


def retire_legacy_otp(apps: StateApps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    connection = schema_editor.connection
    if "otp_totp_totpdevice" not in connection.introspection.table_names():
        return
    with connection.cursor() as cursor:
        cursor.execute("LOCK TABLE otp_totp_totpdevice IN ACCESS EXCLUSIVE MODE")
        cursor.execute("SELECT EXISTS (SELECT 1 FROM otp_totp_totpdevice)")
        if cursor.fetchone()[0]:
            raise RuntimeError(
                "Legacy OTP devices exist. Migrate and verify their factors before upgrading; "
                "the upgrade will not discard an enrolled factor or permit re-enrollment with "
                "only a password."
            )
        cursor.execute("DROP TABLE otp_totp_totpdevice")


class Migration(migrations.Migration):
    dependencies: ClassVar[list[tuple[str, str]]] = [("accounts", "0002_authentication_cache")]

    operations: ClassVar[list[Operation]] = [migrations.RunPython(retire_legacy_otp)]
