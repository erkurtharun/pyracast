import json
import logging
from hashlib import sha256
from io import StringIO

import pytest
from django.conf import LazySettings
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import IntegrityError

from pyracast.connections import operations, secrets
from pyracast.connections.models import ProviderAccount
from pyracast.connections.secrets import decrypt_api_key
from pyracast.regions.models import AuditEvent
from tests.support.concurrency import behind_open_transaction
from tests.support.domain import make_system, provider_key

pytestmark = pytest.mark.django_db(transaction=True)


def solcast_account(admin: User, identity: str, key: str) -> ProviderAccount:
    return operations.create_account(
        admin,
        provider="solcast",
        external_account_id=identity,
        api_key=key,
        authorization_reference="Authorized service use",
    )


def solcast_ids() -> list[int]:
    return list(
        ProviderAccount.objects.filter(provider="solcast")
        .order_by("pk")
        .values_list("pk", flat=True)
    )


def test_a_second_account_with_the_same_key_is_refused_and_names_the_first() -> None:
    system = make_system()
    first = solcast_account(system.admin, "first-owner", provider_key("SHARED"))

    with pytest.raises(ValidationError) as refusal:
        solcast_account(system.admin, "second-owner", provider_key("SHARED"))

    assert "first-owner" in " ".join(refusal.value.messages)
    assert provider_key("SHARED") not in str(refusal.value)
    assert solcast_ids() == [first.pk]


def test_a_key_stored_on_another_account_cannot_replace_a_key() -> None:
    system = make_system()
    solcast_account(system.admin, "first-owner", provider_key("FIRST"))
    second = solcast_account(system.admin, "second-owner", provider_key("SECOND"))

    with pytest.raises(ValidationError, match="first-owner"):
        operations.replace_api_key(
            system.admin,
            second.pk,
            provider_key("FIRST"),
            expected_revision=second.credential_revision,
        )

    second.refresh_from_db()
    assert second.credential_revision == 1
    assert decrypt_api_key(second.encrypted_api_key) == provider_key("SECOND")


def test_the_database_refuses_a_second_account_with_one_key_from_another_client() -> None:
    system = make_system()
    key = provider_key("RACE")

    def first() -> int:
        return solcast_account(system.admin, "first-owner", key).pk

    def second() -> int | ValidationError:
        try:
            return solcast_account(system.admin, "second-owner", key).pk
        except ValidationError as refusal:
            return refusal

    created, refused = behind_open_transaction(first, second)

    assert isinstance(refused, ValidationError)
    assert solcast_ids() == [created]


def test_a_stored_fingerprint_is_unique_in_the_database() -> None:
    system = make_system()
    first = solcast_account(system.admin, "first-owner", provider_key("FIRST"))
    second = solcast_account(system.admin, "second-owner", provider_key("SECOND"))

    with pytest.raises(IntegrityError):
        ProviderAccount.objects.filter(pk=second.pk).update(
            api_key_fingerprint=ProviderAccount.objects.get(pk=first.pk).api_key_fingerprint
        )


def test_a_fingerprint_is_keyed_with_the_application_key(settings: LazySettings) -> None:
    key = provider_key("KEYED")
    fingerprint = secrets.api_key_fingerprints(key)[0]

    assert len(fingerprint) == 64
    assert int(fingerprint, 16) >= 0
    assert fingerprint != sha256(key.encode()).hexdigest()
    settings.SECRET_KEY = provider_key("ANOTHER_APPLICATION_KEY")
    assert secrets.api_key_fingerprints(key)[0] != fingerprint


def test_a_shared_key_stays_refused_while_the_previous_application_key_is_a_fallback(
    settings: LazySettings,
) -> None:
    system = make_system()
    account = solcast_account(system.admin, "first-owner", provider_key("ROTATION"))
    settings.SECRET_KEY_FALLBACKS = [settings.SECRET_KEY]
    settings.SECRET_KEY = provider_key("ROTATED_APPLICATION_KEY")

    with pytest.raises(ValidationError, match="first-owner"):
        solcast_account(system.admin, "second-owner", provider_key("ROTATION"))
    call_command("reencrypt_secrets", stdout=StringIO())

    account.refresh_from_db()
    assert account.api_key_fingerprint == secrets.api_key_fingerprints(provider_key("ROTATION"))[0]
    settings.SECRET_KEY_FALLBACKS = []
    with pytest.raises(ValidationError, match="first-owner"):
        solcast_account(system.admin, "second-owner", provider_key("ROTATION"))


def test_neither_a_key_nor_its_fingerprint_reaches_the_audit_or_the_log(
    pyracast_logs: pytest.LogCaptureFixture,
) -> None:
    pyracast_logs.set_level(logging.DEBUG)
    system = make_system()
    account = solcast_account(system.admin, "first-owner", provider_key("AUDITED"))
    operations.replace_api_key(
        system.admin, account.pk, provider_key("REPLACED"), expected_revision=1
    )
    with pytest.raises(ValidationError) as refusal:
        solcast_account(system.admin, "second-owner", provider_key("REPLACED"))

    account.refresh_from_db()
    audit = json.dumps(list(AuditEvent.objects.values()), default=str)
    for value in (
        provider_key("AUDITED"),
        provider_key("REPLACED"),
        *secrets.api_key_fingerprints(provider_key("AUDITED")),
        str(account.api_key_fingerprint),
    ):
        assert value not in audit
        assert value not in pyracast_logs.text
        assert value not in str(refusal.value)


def test_two_clients_replacing_two_keys_with_one_key_leave_one_holder() -> None:
    system = make_system()
    first = solcast_account(system.admin, "first-owner", provider_key("FIRST"))
    second = solcast_account(system.admin, "second-owner", provider_key("SECOND"))
    key = provider_key("RACE")

    def replace(account: ProviderAccount) -> ValidationError | None:
        try:
            operations.replace_api_key(system.admin, account.pk, key, expected_revision=1)
        except ValidationError as refusal:
            return refusal
        return None

    held, refused = behind_open_transaction(lambda: replace(first), lambda: replace(second))

    assert held is None
    assert isinstance(refused, ValidationError)
    assert decrypt_api_key(ProviderAccount.objects.get(pk=second.pk).encrypted_api_key) == (
        provider_key("SECOND")
    )


def test_a_conflict_other_than_a_shared_key_is_not_reported_as_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = make_system()
    account = solcast_account(system.admin, "first-owner", provider_key("FIRST"))

    def conflict(*args: object, **kwargs: object) -> None:
        raise IntegrityError("another constraint")

    monkeypatch.setattr(ProviderAccount, "save", conflict)
    with pytest.raises(IntegrityError):
        operations.replace_api_key(
            system.admin, account.pk, provider_key("NEW"), expected_revision=1
        )
    monkeypatch.setattr(ProviderAccount.objects, "create", conflict)
    with pytest.raises(IntegrityError):
        solcast_account(system.admin, "second-owner", provider_key("SECOND"))


def test_reencryption_keeps_an_empty_fingerprint_empty(settings: LazySettings) -> None:
    system = make_system()
    account = solcast_account(system.admin, "first-owner", provider_key("FIRST"))
    ProviderAccount.objects.filter(pk=account.pk).update(api_key_fingerprint=None)

    call_command("reencrypt_secrets", stdout=StringIO())

    assert ProviderAccount.objects.get(pk=account.pk).api_key_fingerprint is None
