from io import StringIO

import pytest
from allauth.mfa.models import Authenticator
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from pyracast.accounts.mfa import MFAAdapter
from pyracast.connections.secrets import encrypt_secret, resolve_solcast_secret
from tests.test_domain import make_system

pytestmark = pytest.mark.django_db
OLD_KEY = "test-only-" + "old-master-key"
NEW_KEY = "test-only-" + "new-master-key"


@pytest.mark.parametrize("migrated", [False, True])
def test_secret_rotation_preserves_credentials_and_recovery_code_state(migrated: bool) -> None:
    system = make_system()
    with override_settings(SECRET_KEY=OLD_KEY, SECRET_KEY_FALLBACKS=[]):
        system.account.secret_reference = encrypt_secret("provider-key")
        system.account.save(update_fields=["secret_reference"])
        totp = Authenticator.objects.create(
            user=system.admin, type="totp", data={"secret": MFAAdapter().encrypt("totp-seed")}
        )
        codes = Authenticator.objects.create(
            user=system.admin,
            type="recovery_codes",
            data={"migrated_codes": [MFAAdapter().encrypt("remaining-code")]}
            if migrated
            else {"seed": MFAAdapter().encrypt("recovery-seed"), "used_mask": 3},
        )
    original_revision = system.account.credential_revision
    output = StringIO()
    with override_settings(SECRET_KEY=NEW_KEY, SECRET_KEY_FALLBACKS=[OLD_KEY]):
        call_command("reencrypt_secrets", stdout=output)
    with override_settings(SECRET_KEY=NEW_KEY, SECRET_KEY_FALLBACKS=[]):
        system.account.refresh_from_db()
        totp.refresh_from_db()
        codes.refresh_from_db()
        assert resolve_solcast_secret(system.account.secret_reference) == "provider-key"
        assert system.account.credential_revision == original_revision
        assert MFAAdapter().decrypt(totp.data["secret"]) == "totp-seed"
        if migrated:
            assert MFAAdapter().decrypt(codes.data["migrated_codes"][0]) == "remaining-code"
        else:
            assert MFAAdapter().decrypt(codes.data["seed"]) == "recovery-seed"
            assert codes.data["used_mask"] == 3
    assert output.getvalue() == "Re-encrypted 1 provider credentials and 2 MFA authenticators.\n"


def test_invalid_secret_rolls_back_all_reencryption_without_echoing_values() -> None:
    system = make_system()
    system.account.secret_reference = encrypt_secret("provider-key")
    system.account.save(update_fields=["secret_reference"])
    original = system.account.secret_reference
    Authenticator.objects.create(user=system.admin, type="totp", data={"secret": "corrupted"})
    with pytest.raises(CommandError, match="No changes were committed") as error:
        call_command("reencrypt_secrets", stdout=StringIO())
    assert "corrupted" not in str(error.value)
    system.account.refresh_from_db()
    assert system.account.secret_reference == original


def test_environment_credentials_are_not_rewritten() -> None:
    system = make_system()
    original = system.account.secret_reference
    output = StringIO()
    call_command("reencrypt_secrets", stdout=output)
    system.account.refresh_from_db()
    assert system.account.secret_reference == original
    assert output.getvalue() == "Re-encrypted 0 provider credentials and 0 MFA authenticators.\n"
