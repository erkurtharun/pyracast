import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor

from pyracast.connections.contracts import ProviderError
from pyracast.connections.models import ProviderAccount
from pyracast.connections.operations import create_account
from pyracast.connections.secrets import decrypt_api_key, encrypt_secret
from tests.support.domain import make_system


def test_environment_reference_is_rejected_even_when_the_variable_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLCAST_API_KEY", "test-only-environment-key")
    with pytest.raises(ProviderError):
        decrypt_api_key("env:SOLCAST_API_KEY")


@pytest.mark.django_db(transaction=True)
def test_database_key_migration_preserves_ciphertext_and_clears_fixture_references() -> None:
    previous = [("connections", "0011_canonical_account_identity")]
    executor = MigrationExecutor(connection)
    current = executor.loader.graph.leaf_nodes()
    ciphertext = encrypt_secret("test-only-migration-key")
    try:
        executor.migrate(previous)
        accounts = executor.loader.project_state(previous).apps.get_model(
            "connections", "ProviderAccount"
        )
        live = accounts.objects.create(
            provider="solcast",
            external_account_id="live",
            secret_reference=ciphertext,
            credential_revision=7,
        )
        fixture = accounts.objects.create(
            provider="fixture", external_account_id="fixture", secret_reference="env:" + "FIXTURE"
        )
        MigrationExecutor(connection).migrate(current)
        updated = ProviderAccount.objects.get(pk=live.pk)
        assert updated.encrypted_api_key == ciphertext
        assert updated.credential_revision == 7
        assert decrypt_api_key(updated.encrypted_api_key) == "test-only-migration-key"
        assert ProviderAccount.objects.get(pk=fixture.pk).encrypted_api_key == ""
    finally:
        MigrationExecutor(connection).migrate(current)


@pytest.mark.parametrize("reference", ["env:SOLCAST_API_KEY", "plaintext", ""])
@pytest.mark.django_db(transaction=True)
def test_database_key_migration_blocks_unresolved_live_references(reference: str) -> None:
    previous = [("connections", "0011_canonical_account_identity")]
    executor = MigrationExecutor(connection)
    current = executor.loader.graph.leaf_nodes()
    try:
        executor.migrate(previous)
        accounts = executor.loader.project_state(previous).apps.get_model(
            "connections", "ProviderAccount"
        )
        live = accounts.objects.create(
            provider="solcast", external_account_id="unresolved", secret_reference=reference
        )
        try:
            with pytest.raises(RuntimeError, match="Transfer every Solcast API key") as failure:
                MigrationExecutor(connection).migrate(current)
            assert "SOLCAST_API_KEY" not in str(failure.value)
            assert accounts.objects.get(pk=live.pk).secret_reference == reference
        finally:
            accounts.objects.filter(pk=live.pk).delete()
    finally:
        MigrationExecutor(connection).migrate(current)


@pytest.mark.parametrize(("provider", "value"), [("solcast", ""), ("fixture", "env:FIXTURE")])
@pytest.mark.django_db
def test_database_rejects_unencrypted_provider_keys(provider: str, value: str) -> None:
    with (
        pytest.raises(IntegrityError, match="provider_account_encrypted_api_key"),
        transaction.atomic(),
    ):
        ProviderAccount.objects.create(
            provider=provider, external_account_id="invalid", encrypted_api_key=value
        )


@pytest.mark.django_db
def test_solcast_account_creation_requires_a_database_key() -> None:
    system = make_system()
    with pytest.raises(ValidationError, match="API key is required"):
        create_account(
            system.admin,
            provider="solcast",
            external_account_id="missing-key",
            authorization_reference="test-only-authorized",
        )
    assert not ProviderAccount.objects.filter(provider="solcast").exists()
