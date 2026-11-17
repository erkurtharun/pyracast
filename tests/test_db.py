from concurrent.futures import ThreadPoolExecutor

import pytest
from django.db import DatabaseError, connection, transaction
from django.db.transaction import TransactionManagementError

from pyracast.accounts.models import Tenant
from pyracast.db import read_snapshot

pytestmark = pytest.mark.django_db(transaction=True)


def _setting(name: str) -> str:
    with connection.cursor() as cursor:
        cursor.execute(f"SHOW {name}")
        return str(cursor.fetchone()[0])


def _commit_tenant_elsewhere() -> None:
    def create() -> None:
        try:
            Tenant.objects.create(name="Committed elsewhere")
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(create).result(timeout=10)


def test_read_snapshot_reads_one_committed_state_without_writing() -> None:
    Tenant.objects.create(name="Before")
    with read_snapshot():
        assert _setting("transaction_isolation") == "repeatable read"
        assert _setting("transaction_read_only") == "on"
        assert Tenant.objects.count() == 1
        _commit_tenant_elsewhere()
        assert Tenant.objects.count() == 1
    assert Tenant.objects.count() == 2
    assert connection.get_autocommit()
    assert _setting("transaction_isolation") == "read committed"


def test_read_snapshot_rejects_writes() -> None:
    with pytest.raises(DatabaseError, match="read-only transaction"), read_snapshot():
        Tenant.objects.create(name="Write")
    assert not Tenant.objects.exists()


def test_read_snapshot_must_open_its_own_transaction() -> None:
    with (
        transaction.atomic(),
        pytest.raises(TransactionManagementError, match="own transaction"),
        read_snapshot(),
    ):
        pass
