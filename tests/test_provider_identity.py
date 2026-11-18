from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.contrib.auth.models import User
from django.db import IntegrityError, connection, connections, transaction

from pyracast.connections.models import ProviderAccount
from pyracast.connections.operations import (
    QuotaExceededError,
    create_account,
    reserve_account_request,
)
from pyracast.connections.secrets import decrypt_api_key, encrypt_secret
from pyracast.forecasts.models import RequestPurpose
from pyracast.regions.models import AuditEvent
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
    with pytest.raises(IntegrityError, match="canonical_provider_account"), transaction.atomic():
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
            admin, provider="fixture", external_account_id=identity, api_key=provider_key()
        )
        for identity in ("I", "\u0130", "\u0131")
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


def test_account_index_uses_builtin_unicode_collation() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT c.collname, c.collprovider, i.indisvalid, i.indisunique "
            "FROM pg_index i JOIN pg_class x ON x.oid = i.indexrelid "
            "CROSS JOIN LATERAL unnest(i.indcollation) AS k(collation_oid) "
            "JOIN pg_collation c ON c.oid = k.collation_oid "
            "WHERE x.relname = 'canonical_provider_account'"
        )
        assert cursor.fetchall() == [
            ("pg_unicode_fast", "b", True, True),
            ("pg_unicode_fast", "b", True, True),
        ]
