from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, get_ident
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, connections, transaction
from django.db.models.signals import pre_save
from django.test.utils import CaptureQueriesContext

from pyracast.connections import operations
from pyracast.connections.models import ProviderAccount
from pyracast.connections.operations import create_account
from pyracast.connections.secrets import api_key_fingerprints, decrypt_api_key, encrypt_secret
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


def create_with_one_key(admin: User, identity: str, *, require_new: bool = False) -> int:
    return create_account(
        admin,
        provider="fixture",
        external_account_id=identity,
        api_key=provider_key(),
        require_new=require_new,
    ).pk


def keyed_account(admin: User, identity: str, key: str) -> ProviderAccount:
    return ProviderAccount.objects.create(
        provider="fixture",
        external_account_id=identity,
        encrypted_api_key=encrypt_secret(key),
        api_key_fingerprint=api_key_fingerprints(key)[0],
        authorization_reference="race",
        authorized_by=admin,
        authorized_at=NOW,
    )


def move_key(identity: str, key: str) -> None:
    ProviderAccount.objects.filter(external_account_id=identity).update(
        encrypted_api_key=encrypt_secret(key), api_key_fingerprint=api_key_fingerprints(key)[0]
    )


def commit_around_lookups(
    monkeypatch: pytest.MonkeyPatch,
    after_lookup: Callable[[], object],
    at_recovery: Callable[[], object] | None = None,
) -> list[Any]:
    look = operations._requested_account_or_refusal
    caller = get_ident()
    committed: list[Any] = []

    def interleave(*args: Any, **kwargs: Any) -> ProviderAccount | None:
        if get_ident() != caller:
            return look(*args, **kwargs)
        if not committed:
            found = look(*args, **kwargs)
            committed.append(committed_elsewhere(after_lookup))
            return found
        if at_recovery is not None:
            committed.append(committed_elsewhere(at_recovery))
        return look(*args, **kwargs)

    monkeypatch.setattr(operations, "_requested_account_or_refusal", interleave)
    return committed


def test_an_equivalent_identity_committed_after_the_lookup_is_returned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin = User.objects.create(username="admin", is_staff=True, is_superuser=True)
    committed = commit_around_lookups(monkeypatch, lambda: create_with_one_key(admin, "STRASSE"))

    returned = create_with_one_key(admin, "Straße")

    assert returned == committed[0]
    assert ProviderAccount.objects.count() == 1
    assert AuditEvent.objects.filter(action="provider_account_created").count() == 1


def test_another_identity_committed_after_the_lookup_keeps_the_key_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin = User.objects.create(username="admin", is_staff=True, is_superuser=True)
    committed = commit_around_lookups(monkeypatch, lambda: create_with_one_key(admin, "STRASSE"))

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


def key_holder() -> str:
    return ProviderAccount.objects.get(
        api_key_fingerprint=api_key_fingerprints(provider_key())[0]
    ).external_account_id


def test_the_requested_identity_given_the_key_during_recovery_is_returned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin = User.objects.create(username="admin", is_staff=True, is_superuser=True)

    def move_the_key_to_the_requested_identity() -> ProviderAccount:
        move_key("other", provider_key("OTHER"))
        return keyed_account(admin, "strasse", provider_key())

    committed = commit_around_lookups(
        monkeypatch,
        lambda: keyed_account(admin, "other", provider_key()),
        move_the_key_to_the_requested_identity,
    )

    returned = create_with_one_key(admin, "Straße")

    assert returned == committed[1].pk
    assert key_holder() == "strasse"
    assert ProviderAccount.objects.count() == 2
    assert not AuditEvent.objects.filter(action="provider_account_created").exists()


@pytest.mark.parametrize("require_new", [False, True])
def test_the_requested_identity_created_during_recovery_takes_precedence_over_the_key_holder(
    monkeypatch: pytest.MonkeyPatch, require_new: bool
) -> None:
    admin = User.objects.create(username="admin", is_staff=True, is_superuser=True)
    committed = commit_around_lookups(
        monkeypatch,
        lambda: keyed_account(admin, "other", provider_key()),
        lambda: keyed_account(admin, "strasse", provider_key("OTHER")),
    )

    if require_new:
        with pytest.raises(ConflictError):
            create_with_one_key(admin, "Straße", require_new=True)
    else:
        assert create_with_one_key(admin, "Straße") == committed[1].pk

    assert key_holder() == "other"
    assert ProviderAccount.objects.count() == 2
    assert not AuditEvent.objects.filter(action="provider_account_created").exists()


def test_a_key_moved_to_a_third_account_during_recovery_is_refused_with_its_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin = User.objects.create(username="admin", is_staff=True, is_superuser=True)

    def move_the_key_to_a_third_account() -> None:
        move_key("other", provider_key("OTHER"))
        keyed_account(admin, "third", provider_key())

    commit_around_lookups(
        monkeypatch,
        lambda: keyed_account(admin, "other", provider_key()),
        move_the_key_to_a_third_account,
    )

    with pytest.raises(ValidationError, match="third"):
        create_with_one_key(admin, "Straße")

    assert key_holder() == "third"
    assert ProviderAccount.objects.count() == 2
    assert not AuditEvent.objects.filter(action="provider_account_created").exists()


def test_recovery_that_finds_neither_account_raises_the_unique_violation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin = User.objects.create(username="admin", is_staff=True, is_superuser=True)
    commit_around_lookups(
        monkeypatch,
        lambda: keyed_account(admin, "other", provider_key()),
        lambda: move_key("other", provider_key("OTHER")),
    )

    with pytest.raises(IntegrityError, match="provideraccount_api_key_fingerprint_uniq"):
        create_with_one_key(admin, "Straße")

    assert list(ProviderAccount.objects.values_list("external_account_id", flat=True)) == ["other"]
    assert not AuditEvent.objects.filter(action="provider_account_created").exists()


def test_recovery_from_a_lost_insert_reads_the_accounts_once() -> None:
    admin = User.objects.create(username="admin", is_staff=True, is_superuser=True)
    raced: list[None] = []

    def insert_competitor(**_: object) -> None:
        if not raced:
            raced.append(None)
            committed_elsewhere(lambda: keyed_account(admin, "other", provider_key()))

    pre_save.connect(insert_competitor, sender=ProviderAccount, weak=False)
    try:
        with (
            CaptureQueriesContext(connection) as queries,
            pytest.raises(ValidationError, match="other"),
        ):
            create_with_one_key(admin, "Straße")
    finally:
        pre_save.disconnect(insert_competitor, sender=ProviderAccount)

    statements = [query["sql"] for query in queries.captured_queries]
    insert = next(
        index
        for index, sql in enumerate(statements)
        if sql.startswith('INSERT INTO "connections_provideraccount"')
    )
    recovery_reads = [
        sql
        for sql in statements[insert + 1 :]
        if sql.startswith("SELECT") and '"connections_provideraccount"' in sql
    ]
    assert len(recovery_reads) == 1


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
