import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from pyracast.connections.contracts import ProviderError
from pyracast.connections.models import ProviderAccount
from pyracast.connections.operations import create_account
from pyracast.connections.secrets import decrypt_api_key
from tests.support.domain import make_system


def test_environment_reference_is_rejected_even_when_the_variable_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLCAST_API_KEY", "test-only-environment-key")
    with pytest.raises(ProviderError):
        decrypt_api_key("env:SOLCAST_API_KEY")


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
