from datetime import date

import httpx
import pytest
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import override_settings

from pyracast.connections.contracts import ProviderFailure
from pyracast.connections.operations import create_account, rotate_secret
from pyracast.connections.secrets import (
    encrypt_secret,
    resolve_solcast_secret,
    validate_secret_reference,
)
from pyracast.providers import SolcastProvider
from pyracast.regions.models import AuditEvent
from tests.test_providers import binding_for, payload_for

KEY = "test-only-" + "provider-credential"


def test_encryption_preserves_key_without_plaintext_storage_and_randomizes_ciphertext() -> None:
    first = encrypt_secret(KEY)
    assert KEY not in first
    assert encrypt_secret(KEY) != first
    assert resolve_solcast_secret(first) == KEY
    validate_secret_reference(first)
    longest = encrypt_secret("x" * 1024)
    assert len(longest) <= 2048
    assert resolve_solcast_secret(longest) == "x" * 1024


@pytest.mark.parametrize("raw", ["", "a b", "a\nb", "x" * 1025, "şifre"])
def test_invalid_keys_are_rejected(raw: str) -> None:
    with pytest.raises(ValidationError):
        encrypt_secret(raw)


@pytest.mark.parametrize(
    "reference", ["encrypted:v1:invalid", "encrypted:v1:" + "x" * 2048, "plaintext"]
)
def test_invalid_references_are_rejected_without_echoing_secret(reference: str) -> None:
    with pytest.raises(ValidationError) as failure:
        validate_secret_reference(reference)
    assert reference not in str(failure.value)


def test_rotation_retains_old_keys_only_while_fallback_is_configured() -> None:
    old = "test-only-" + "old-master-key"
    new = "test-only-" + "new-master-key"
    with override_settings(SECRET_KEY=old, SECRET_KEY_FALLBACKS=[]):
        original = encrypt_secret(KEY)
    with override_settings(SECRET_KEY=new, SECRET_KEY_FALLBACKS=[old]):
        assert resolve_solcast_secret(original) == KEY
        replacement = encrypt_secret(resolve_solcast_secret(original))
    with override_settings(SECRET_KEY=new, SECRET_KEY_FALLBACKS=[]):
        assert resolve_solcast_secret(replacement) == KEY
        with pytest.raises(ProviderFailure, match="invalid_provider_secret"):
            resolve_solcast_secret(original)


def test_encrypted_provider_request_uses_plaintext_only_in_authorization_header() -> None:
    binding = binding_for()
    binding.provider_account.secret_reference = encrypt_secret(KEY)

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {KEY}"
        assert KEY not in str(request.url)
        return httpx.Response(200, json=payload_for())

    result = SolcastProvider(httpx.MockTransport(respond)).fetch(binding, date(2026, 11, 10))
    assert KEY not in str(result.raw_payload)


@pytest.mark.django_db
def test_account_create_and_rotation_keep_encrypted_keys_out_of_audit_records() -> None:
    admin = User.objects.create_user(username="secret-admin", is_superuser=True)
    account = create_account(
        admin,
        provider="solcast",
        external_account_id="test-account",
        secret_reference=encrypt_secret(KEY),
        authorization_reference="test-only-authorized",
    )
    account.refresh_from_db()
    assert KEY not in account.secret_reference
    rotate_secret(admin, account.pk, encrypt_secret(KEY + "-rotated"))
    account.refresh_from_db()
    assert resolve_solcast_secret(account.secret_reference) == KEY + "-rotated"
    assert account.credential_revision == 2
    assert KEY not in str(list(AuditEvent.objects.values()))
