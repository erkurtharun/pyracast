from collections.abc import Iterator
from importlib import import_module

import pytest
from django.apps import apps
from django.db import connection

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def legacy_table() -> Iterator[None]:
    with connection.cursor() as cursor:
        cursor.execute("CREATE TABLE otp_totp_totpdevice (id bigint PRIMARY KEY)")
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute("DROP TABLE IF EXISTS otp_totp_totpdevice")


def retire_legacy_table() -> None:
    migration = import_module("pyracast.accounts.migrations.0003_retire_legacy_otp")
    with connection.schema_editor() as schema_editor:
        migration.retire_legacy_otp(apps, schema_editor)


def test_upgrade_without_legacy_otp_requires_no_cleanup() -> None:
    retire_legacy_table()
    assert "otp_totp_totpdevice" not in connection.introspection.table_names()


@pytest.mark.usefixtures("legacy_table")
def test_upgrade_removes_only_empty_legacy_otp_table() -> None:
    retire_legacy_table()
    assert "otp_totp_totpdevice" not in connection.introspection.table_names()


@pytest.mark.usefixtures("legacy_table")
def test_upgrade_refuses_to_discard_existing_mfa_factors() -> None:
    with connection.cursor() as cursor:
        cursor.execute("INSERT INTO otp_totp_totpdevice (id) VALUES (1)")
    with pytest.raises(RuntimeError, match="will not discard an enrolled factor"):
        retire_legacy_table()
    with connection.cursor() as cursor:
        cursor.execute("SELECT id FROM otp_totp_totpdevice")
        assert cursor.fetchall() == [(1,)]
