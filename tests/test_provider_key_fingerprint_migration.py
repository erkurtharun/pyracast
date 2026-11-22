from collections.abc import Iterator

import pytest
from django.apps.registry import Apps
from django.contrib.auth.models import User
from django.core.management.base import CommandError
from django.db import IntegrityError, connection, models
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.recorder import MigrationRecorder
from django.test import override_settings

from pyracast.connections import secrets
from pyracast.connections.models import ProviderAccount
from pyracast.connections.secrets import encrypt_secret
from tests.support.domain import provider_key
from tests.support.migrations import constraints, forward, migrate

pytestmark = pytest.mark.django_db(transaction=True)

PREVIOUS = ("connections", "0004_schema_naming_convention")
UNIQUE = "provideraccount_api_key_fingerprint_uniq"


@pytest.fixture
def previous() -> Iterator[Apps]:
    migrate(PREVIOUS)
    try:
        yield MigrationExecutor(connection).loader.project_state(PREVIOUS).apps
    finally:
        forward()


def stored(apps: Apps, identity: str, encrypted: str, provider: str = "solcast") -> int:
    model: type[models.Model] = apps.get_model("connections", "ProviderAccount")
    admin = User.objects.get_or_create(username="admin", is_superuser=True, is_staff=True)[0]
    return int(
        model._default_manager.create(
            provider=provider,
            external_account_id=identity,
            encrypted_api_key=encrypted,
            authorization_reference="Authorized service use",
            authorized_by_id=admin.pk,
        ).pk
    )


def fingerprint(pk: int) -> str | None:
    return ProviderAccount.objects.get(pk=pk).api_key_fingerprint


def test_every_readable_key_gets_its_fingerprint_and_a_shared_key_is_left_empty_and_listed(
    previous: Apps, capsys: pytest.CaptureFixture[str]
) -> None:
    first = stored(previous, "first", encrypt_secret(provider_key("SHARED")))
    other = stored(previous, "other", encrypt_secret(provider_key("OTHER")))
    duplicate = stored(previous, "duplicate", encrypt_secret(provider_key("SHARED")))
    fixture = stored(previous, "fixture", "", provider="fixture")

    forward()

    assert fingerprint(first) == secrets.api_key_fingerprints(provider_key("SHARED"))[0]
    assert fingerprint(other) == secrets.api_key_fingerprints(provider_key("OTHER"))[0]
    assert fingerprint(duplicate) is None
    assert fingerprint(fixture) is None
    assert UNIQUE in constraints("connections_provideraccount")
    output = capsys.readouterr().out
    assert f"provider account(s) {duplicate} share an API key" in output
    assert provider_key("SHARED") not in output
    assert str(fingerprint(first)) not in output


def test_a_key_the_application_key_cannot_read_is_left_empty_and_listed(
    previous: Apps, capsys: pytest.CaptureFixture[str]
) -> None:
    with override_settings(SECRET_KEY=provider_key("RETIRED_APPLICATION_KEY")):
        unreadable = stored(previous, "retired", encrypt_secret(provider_key("RETIRED")))
    readable = stored(previous, "current", encrypt_secret(provider_key("CURRENT")))

    forward()

    assert fingerprint(unreadable) is None
    assert fingerprint(readable) == secrets.api_key_fingerprints(provider_key("CURRENT"))[0]
    assert f"provider account(s) {unreadable} hold a key" in capsys.readouterr().out


def test_a_database_without_accounts_migrates_silently(
    previous: Apps, capsys: pytest.CaptureFixture[str]
) -> None:
    forward()

    assert UNIQUE in constraints("connections_provideraccount")
    assert "provider account" not in capsys.readouterr().out


def test_without_the_application_key_the_migration_stops_with_one_line_and_changes_nothing(
    previous: Apps,
) -> None:
    account = stored(previous, "first", encrypt_secret(provider_key("FIRST")))

    with override_settings(SECRET_KEY=""), pytest.raises(CommandError) as refusal:
        forward()

    message = str(refusal.value)
    assert "\n" not in message
    assert "PYRACAST_SECRET_KEY" in message
    with connection.cursor() as cursor:
        columns = {
            column.name
            for column in connection.introspection.get_table_description(
                cursor, "connections_provideraccount"
            )
        }
    assert "api_key_fingerprint" not in columns
    assert not MigrationRecorder.Migration.objects.filter(
        app="connections", name__startswith="0005"
    ).exists()
    forward()
    assert fingerprint(account) == secrets.api_key_fingerprints(provider_key("FIRST"))[0]


def test_after_the_migration_a_second_row_with_one_fingerprint_is_refused(
    previous: Apps,
) -> None:
    first = stored(previous, "first", encrypt_secret(provider_key("FIRST")))
    second = stored(previous, "second", encrypt_secret(provider_key("SECOND")))
    forward()

    with pytest.raises(IntegrityError):
        ProviderAccount.objects.filter(pk=second).update(api_key_fingerprint=fingerprint(first))
