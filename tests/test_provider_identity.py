from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, connections, transaction
from django.db.models.signals import pre_save

from pyracast.connections import operations
from pyracast.connections.models import ProviderAccount
from pyracast.connections.operations import create_account
from pyracast.connections.secrets import decrypt_api_key, encrypt_secret
from pyracast.errors import ConflictError
from pyracast.forecasts.models import RequestPurpose
from pyracast.forecasts.quota import QuotaExceededError, reserve_account_request
from pyracast.regions.models import AuditEvent
from tests.support.concurrency import committed_elsewhere, paused_in
from tests.support.domain import NOW, provider_key

pytestmark = pytest.mark.django_db(transaction=True)

IDENTITIES = [
    ("I", "i", "i"),
    ("\u0130", "i\u0307", "i\u0307"),
    ("\u0131", "\u0131", "\u0131"),
    ("Stra\u00dfe", "STRASSE", "strasse"),
    ("\u1e9e", "ss", "ss"),
    ("\u03a3", "\u03c2", "\u03c3"),
    ("\t\xa0Identity\u2003\u3000\x1c", "identity", "identity"),
]


@pytest.mark.parametrize(("original", "equivalent", "expected"), IDENTITIES)
def test_raw_provider_variants_share_one_account_and_quota(
    original: str, equivalent: str, expected: str
) -> None:
    admin = User.objects.create(username="admin", is_staff=True, is_superuser=True)
    existing = ProviderAccount.objects.create(
        provider="fixture",
        external_account_id=original,
        encrypted_api_key=encrypt_secret(provider_key()),
        daily_quota=1,
        authorized_by=admin,
        authorized_at=NOW,
    )
    duplicate = create_account(
        admin,
        provider="\xa0FIXTURE\t",
        external_account_id=equivalent,
        api_key=provider_key("OTHER"),
        daily_quota=10,
    )
    assert duplicate.pk == existing.pk
    assert duplicate.provider == "fixture"
    assert decrypt_api_key(duplicate.encrypted_api_key) == provider_key()
    assert duplicate.daily_quota == 1
    assert not AuditEvent.objects.filter(action="provider_account_created").exists()
    reserve_account_request(existing, NOW, purpose=RequestPurpose.VALIDATION)
    with pytest.raises(QuotaExceededError):
        reserve_account_request(duplicate, NOW, purpose=RequestPurpose.VALIDATION)
    with (
        pytest.raises(IntegrityError, match="provideraccount_provider_external_account_id_uniq"),
        transaction.atomic(),
    ):
        ProviderAccount.objects.create(
            provider="fixture",
            external_account_id=equivalent,
            encrypted_api_key=encrypt_secret(provider_key("OTHER")),
        )
    assert ProviderAccount.objects.count() == 1
    assert original.strip().casefold() == expected


@pytest.mark.parametrize(("original", "equivalent", "expected"), IDENTITIES)
def test_new_account_identity_uses_database_unicode_casefold(
    original: str, equivalent: str, expected: str
) -> None:
    admin = User.objects.create(username="admin", is_staff=True, is_superuser=True)
    created = create_account(
        admin, provider="fixture", external_account_id=original, api_key=provider_key()
    )
    assert created.external_account_id == expected
    assert created.provider == "fixture"
    assert original.strip().casefold() == expected
    assert (
        create_account(
            admin, provider="fixture", external_account_id=equivalent, api_key=provider_key()
        ).pk
        == created.pk
    )


def test_dotted_and_dotless_turkish_identities_keep_distinct_accounts() -> None:
    admin = User.objects.create(username="admin", is_staff=True, is_superuser=True)
    accounts = [
        create_account(
            admin, provider="fixture", external_account_id=identity, api_key=provider_key(name)
        )
        for identity, name in (("I", "LATIN"), ("\u0130", "DOTTED"), ("\u0131", "DOTLESS"))
    ]
    assert len({account.pk for account in accounts}) == 3
    assert [account.external_account_id for account in accounts] == ["i", "i\u0307", "\u0131"]


def test_concurrent_unicode_account_creation_keeps_one_quota_identity() -> None:
    admin = User.objects.create(username="admin", is_staff=True, is_superuser=True)
    barrier = Barrier(2)

    def create(identity: str) -> int:
        try:
            barrier.wait(timeout=10)
            return create_account(
                admin,
                provider="fixture",
                external_account_id=identity,
                api_key=provider_key(),
                daily_quota=1,
            ).pk
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        identifiers = list(pool.map(create, ["Stra\u00dfe", "STRASSE"]))
    assert identifiers[0] == identifiers[1]
    assert ProviderAccount.objects.count() == 1
    assert AuditEvent.objects.filter(action="provider_account_created").count() == 1


def create_with_one_key(admin: User, identity: str) -> int:
    return create_account(
        admin, provider="fixture", external_account_id=identity, api_key=provider_key()
    ).pk


def commit_before_key_check(
    monkeypatch: pytest.MonkeyPatch, admin: User, identity: str
) -> list[int]:
    refuse = operations._refuse_shared_key
    committed: list[int] = []

    def commit_then_refuse(*args: Any, **kwargs: Any) -> None:
        if not committed:
            committed.append(0)
            committed[0] = committed_elsewhere(lambda: create_with_one_key(admin, identity))
        refuse(*args, **kwargs)

    monkeypatch.setattr(operations, "_refuse_shared_key", commit_then_refuse)
    return committed


def test_an_equivalent_identity_committed_after_the_lookup_is_returned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin = User.objects.create(username="admin", is_staff=True, is_superuser=True)
    committed = commit_before_key_check(monkeypatch, admin, "STRASSE")

    returned = create_with_one_key(admin, "Straße")

    assert returned == committed[0]
    assert ProviderAccount.objects.count() == 1
    assert AuditEvent.objects.filter(action="provider_account_created").count() == 1


def test_another_identity_committed_after_the_lookup_keeps_the_key_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin = User.objects.create(username="admin", is_staff=True, is_superuser=True)
    committed = commit_before_key_check(monkeypatch, admin, "STRASSE")

    with pytest.raises(ValidationError, match="strasse"):
        create_with_one_key(admin, "other")

    assert list(ProviderAccount.objects.values_list("pk", flat=True)) == committed
    assert AuditEvent.objects.filter(action="provider_account_created").count() == 1


def test_another_identity_inserted_before_the_commit_keeps_the_key_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin = User.objects.create(username="admin", is_staff=True, is_superuser=True)

    with pytest.raises(ValidationError, match="strasse"):
        paused_in(
            monkeypatch,
            operations,
            "record_audit",
            lambda: create_with_one_key(admin, "STRASSE"),
            lambda: create_with_one_key(admin, "other"),
        )

    assert list(ProviderAccount.objects.values_list("external_account_id", flat=True)) == [
        "strasse"
    ]
    assert AuditEvent.objects.filter(action="provider_account_created").count() == 1


def test_account_index_uses_builtin_unicode_collation() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT c.collname, c.collprovider, i.indisvalid, i.indisunique "
            "FROM pg_index i JOIN pg_class x ON x.oid = i.indexrelid "
            "CROSS JOIN LATERAL unnest(i.indcollation) AS k(collation_oid) "
            "JOIN pg_collation c ON c.oid = k.collation_oid "
            "WHERE x.relname = 'provideraccount_provider_external_account_id_uniq'"
        )
        assert cursor.fetchall() == [
            ("pg_unicode_fast", "b", True, True),
            ("pg_unicode_fast", "b", True, True),
        ]


@pytest.mark.parametrize("require_new", [False, True])
def test_account_created_by_a_racing_writer_is_returned_or_refused(require_new: bool) -> None:
    admin = User.objects.create(username="admin", is_staff=True, is_superuser=True)
    raced: list[int] = []

    def insert_in_own_connection() -> int:
        try:
            return ProviderAccount.objects.create(
                provider="fixture",
                external_account_id="STRASSE",
                authorization_reference="race",
                authorized_by=admin,
                authorized_at=NOW,
            ).pk
        finally:
            connections.close_all()

    def insert_competitor(**_: object) -> None:
        if raced:
            return
        raced.append(0)
        with ThreadPoolExecutor(max_workers=1) as pool:
            raced[0] = pool.submit(insert_in_own_connection).result()

    pre_save.connect(insert_competitor, sender=ProviderAccount, weak=False)
    try:
        if require_new:
            with pytest.raises(ConflictError):
                create_account(
                    admin,
                    provider="fixture",
                    external_account_id="Straße",
                    api_key=provider_key(),
                    daily_quota=1,
                    require_new=True,
                )
        else:
            returned = create_account(
                admin,
                provider="fixture",
                external_account_id="Straße",
                api_key=provider_key(),
                daily_quota=1,
            )
            assert returned.pk == raced[0]
    finally:
        pre_save.disconnect(insert_competitor, sender=ProviderAccount)
    assert ProviderAccount.objects.count() == 1
    assert not AuditEvent.objects.filter(action="provider_account_created").exists()
