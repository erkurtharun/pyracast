import pytest
from allauth.mfa.adapter import get_adapter
from allauth.mfa.models import Authenticator
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.test import override_settings

from pyracast.accounts.mfa import KEY_CONTEXT, PREFIX, MFAAdapter
from pyracast.connections.secrets import encrypt_secret
from pyracast.encryption import secret_cipher

SECRET = "test-only-" + "authenticator-seed"


def test_configured_mfa_adapter_encrypts_and_randomizes_authenticator_secrets() -> None:
    adapter = get_adapter()
    ciphertext = adapter.encrypt(SECRET)
    assert SECRET not in ciphertext
    assert adapter.encrypt(SECRET) != ciphertext
    assert adapter.decrypt(ciphertext) == SECRET


def test_mfa_key_rotation_requires_fallback_until_secrets_are_reencrypted() -> None:
    adapter = MFAAdapter()
    old_key, new_key = "test-only-old-master-key", "test-only-new-master-key"
    with override_settings(SECRET_KEY=old_key, SECRET_KEY_FALLBACKS=[]):
        ciphertext = adapter.encrypt(SECRET)
    with override_settings(SECRET_KEY=new_key, SECRET_KEY_FALLBACKS=[old_key]):
        assert adapter.decrypt(ciphertext) == SECRET
        replacement = adapter.encrypt(adapter.decrypt(ciphertext))
    with override_settings(SECRET_KEY=new_key, SECRET_KEY_FALLBACKS=[]):
        assert adapter.decrypt(replacement) == SECRET
        with pytest.raises(PermissionDenied, match="Authenticator could not be verified"):
            adapter.decrypt(ciphertext)


@pytest.mark.parametrize("value", [SECRET, PREFIX + "invalid", ""])
def test_mfa_rejects_plaintext_and_corrupted_secrets_without_echoing_them(value: str) -> None:
    with pytest.raises(PermissionDenied) as error:
        MFAAdapter().decrypt(value)
    assert str(error.value) == "Authenticator could not be verified."


def test_mfa_keys_are_separated_from_provider_credential_keys() -> None:
    provider_ciphertext = encrypt_secret(SECRET).removeprefix("encrypted:v1:")
    with pytest.raises(PermissionDenied):
        MFAAdapter().decrypt(PREFIX + provider_ciphertext)


def test_mfa_rejects_authenticated_non_text_payload() -> None:
    ciphertext = secret_cipher(KEY_CONTEXT).encrypt(b"\xff").decode("ascii")
    with pytest.raises(PermissionDenied):
        MFAAdapter().decrypt(PREFIX + ciphertext)


@pytest.mark.parametrize(
    ("staff", "superuser", "kind", "allowed"),
    [
        (True, False, "totp", False),
        (False, True, "totp", False),
        (False, False, "totp", True),
        (True, True, "recovery_codes", True),
    ],
)
def test_administrators_cannot_remove_their_required_factor(
    staff: bool, superuser: bool, kind: str, allowed: bool
) -> None:
    authenticator = Authenticator(
        user=User(is_staff=staff, is_superuser=superuser), type=kind, data={}
    )
    assert MFAAdapter().can_delete_authenticator(authenticator) is allowed
