from io import StringIO
from typing import Any

import pytest
from allauth.mfa.models import Authenticator
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import override_settings

from pyracast.accounts.management.commands import reencrypt_secrets
from pyracast.accounts.mfa import MFAAdapter
from pyracast.connections.models import ProviderAccount
from pyracast.connections.secrets import KEY_CONTEXT, PREFIX, encrypt_secret
from pyracast.encryption import secret_cipher
from tests.support.domain import System, make_system

pytestmark = pytest.mark.django_db
OLD_KEY = "test-only-" + "old-master-key"
NEW_KEY = "test-only-" + "new-master-key"
OTHER_KEY = "test-only-" + "unrelated-master-key"
ROTATING = {"SECRET_KEY": NEW_KEY, "SECRET_KEY_FALLBACKS": [OLD_KEY]}
MALFORMED = [
    ("totp", []),
    ("totp", {}),
    ("totp", {"secret": 5}),
    ("recovery_codes", {"migrated_codes": "mfa:v1:x"}),
    ("recovery_codes", {"migrated_codes": [7]}),
    ("recovery_codes", {"seed": None}),
    ("recovery_codes", "seed"),
]


def stored(key: str, value: str = "provider-key") -> tuple[System, Authenticator]:
    system = make_system()
    with override_settings(SECRET_KEY=key, SECRET_KEY_FALLBACKS=[]):
        system.account.encrypted_api_key = encrypt_secret(value)
        system.account.save(update_fields=["encrypted_api_key"])
        totp = Authenticator.objects.create(
            user=system.admin, type="totp", data={"secret": MFAAdapter().encrypt("totp-seed")}
        )
    return system, totp


def check(*arguments: str) -> tuple[int, str]:
    output = StringIO()
    try:
        call_command("check_secrets", *arguments, stdout=output)
    except CommandError as error:
        return error.returncode, f"{output.getvalue()}{error}"
    return 0, output.getvalue()


def rows() -> dict[str, Any]:
    return {
        "providers": list(ProviderAccount.objects.values_list("pk", "encrypted_api_key")),
        "authenticators": list(Authenticator.objects.values_list("pk", "data")),
    }


def test_check_secrets_passes_when_every_secret_decrypts_with_the_primary_key() -> None:
    stored(NEW_KEY)

    with override_settings(**ROTATING):
        assert check() == (
            0,
            "connections_provideraccount: 1 primary, 0 fallback, 0 unreadable\n"
            "mfa_authenticator: 1 primary, 0 fallback, 0 unreadable\n",
        )
        assert check("--require-primary")[0] == 0


def test_check_secrets_counts_fallback_secrets_and_refuses_them_when_primary_is_required() -> None:
    stored(OLD_KEY)

    with override_settings(**ROTATING):
        assert check()[0] == 0
        status, output = check("--require-primary")

    assert status == 2
    assert "connections_provideraccount: 0 primary, 1 fallback, 0 unreadable" in output
    assert "re-encrypt" in output


def test_check_secrets_names_unreadable_rows_without_their_values() -> None:
    system, totp = stored(OTHER_KEY, value="provider-plaintext")
    account = system.account

    with override_settings(**ROTATING):
        status, output = check()

    assert status == 1
    assert f"Unreadable: connections_provideraccount {account.pk}" in output
    assert f"Unreadable: mfa_authenticator {totp.pk}" in output
    assert "provider-plaintext" not in output
    assert account.encrypted_api_key not in output


@pytest.mark.parametrize(("kind", "data"), MALFORMED)
def test_check_secrets_reports_a_malformed_authenticator_as_unreadable(
    kind: str, data: Any
) -> None:
    system = make_system()
    authenticator = Authenticator.objects.create(user=system.admin, type=kind, data=data)

    status, output = check()

    assert status == 1
    assert f"Unreadable: mfa_authenticator {authenticator.pk}" in output


def test_check_secrets_reads_only_and_ignores_empty_and_other_authenticators() -> None:
    system = make_system()
    Authenticator.objects.create(user=system.admin, type="webauthn", data={"credential": "x"})
    before = rows()

    assert check()[0] == 0
    assert rows() == before


def test_check_secrets_on_a_database_without_the_tables_has_nothing_to_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(connection.introspection, "table_names", lambda *args, **kwargs: [])

    assert check() == (0, "No stored secrets: the tables do not exist yet.\n")


@pytest.mark.parametrize("missing", ["connections_provideraccount", "mfa_authenticator"])
def test_check_secrets_refuses_a_schema_that_holds_only_one_secret_table(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    stored(OTHER_KEY)
    present = [name for name in connection.introspection.table_names() if name != missing]
    monkeypatch.setattr(connection.introspection, "table_names", lambda *args, **kwargs: present)

    status, output = check()

    assert status == 1
    assert f"{missing} does not exist" in output


@pytest.mark.parametrize(("kind", "data"), MALFORMED)
def test_reencryption_of_a_malformed_shape_leaves_every_row_unchanged(kind: str, data: Any) -> None:
    system, _ = stored(OLD_KEY)
    malformed = Authenticator.objects.create(user=system.member, type=kind, data=data)
    before = rows()

    with override_settings(**ROTATING), pytest.raises(CommandError) as error:
        call_command("reencrypt_secrets", stdout=StringIO())

    assert error.value.returncode == 1
    assert f"mfa_authenticator {malformed.pk}" in str(error.value)
    assert rows() == before


def test_verified_reencryption_leaves_every_secret_readable_with_the_primary_key_alone() -> None:
    stored(OLD_KEY)
    output = StringIO()

    with override_settings(**ROTATING):
        call_command("reencrypt_secrets", "--verify", stdout=output)
    with override_settings(SECRET_KEY=NEW_KEY, SECRET_KEY_FALLBACKS=[]):
        assert check("--require-primary")[0] == 0

    assert output.getvalue() == (
        "Re-encrypted 1 provider credentials and 1 MFA authenticators.\n"
        "Verified: every stored secret decrypts with the primary key alone.\n"
    )


def test_verification_failure_rolls_back_the_reencryption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, _ = stored(OLD_KEY)
    account = system.account
    before = rows()
    monkeypatch.setattr(reencrypt_secrets, "encrypt_secret", lambda _raw: account.encrypted_api_key)

    with override_settings(**ROTATING), pytest.raises(CommandError) as error:
        call_command("reencrypt_secrets", "--verify", stdout=StringIO())

    assert error.value.returncode == 2
    assert f"connections_provideraccount {account.pk}" in str(error.value)
    assert rows() == before


def test_reencryption_refuses_a_stored_key_it_could_no_longer_write() -> None:
    system = make_system()
    with override_settings(SECRET_KEY=OLD_KEY, SECRET_KEY_FALLBACKS=[]):
        system.account.encrypted_api_key = PREFIX + secret_cipher(KEY_CONTEXT).encrypt(
            b"key with spaces"
        ).decode("ascii")
        system.account.save(update_fields=["encrypted_api_key"])
    before = rows()

    with override_settings(**ROTATING), pytest.raises(CommandError) as error:
        call_command("reencrypt_secrets", stdout=StringIO())

    assert error.value.returncode == 1
    assert f"connections_provideraccount {system.account.pk}" in str(error.value)
    assert "key with spaces" not in str(error.value)
    assert rows() == before
