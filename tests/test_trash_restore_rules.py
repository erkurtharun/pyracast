import ast
import logging
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from uuid import UUID

import pytest
from django.core.mail import EmailMultiAlternatives
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from pyracast import trash
from pyracast.accounts.models import Membership
from pyracast.mail.errors import NotSentError, PermanentlyNotSentError
from pyracast.notifications import jobs
from pyracast.notifications.jobs import process_notification
from pyracast.notifications.models import NotificationDelivery
from pyracast.plants.models import Plant
from pyracast.regions.models import Region
from tests.support.domain import NOW, System
from tests.support.notifications import SLOT, ready
from tests.support.plant_operations import make_plant
from tests.support.trash_operations import sibling

pytestmark = pytest.mark.django_db(transaction=True)

SOURCE = Path(__file__).resolve().parents[1] / "src" / "pyracast"


def _expired(system: System, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(timezone, "now", lambda: NOW + trash.RETENTION)


def _location_taken(system: System, monkeypatch: pytest.MonkeyPatch) -> None:
    other, _ = sibling(system)
    Region.objects.filter(pk=other.pk).update(
        latitude=system.region.latitude + 0.0000004, longitude=system.region.longitude
    )


def _purge_started(system: System, monkeypatch: pytest.MonkeyPatch) -> None:
    Region.objects.filter(pk=system.region.pk).update(purge_started_at=NOW)


BLOCKERS: dict[str, tuple[list[Callable[[System, pytest.MonkeyPatch], None]], str | None]] = {
    "restorable": ([], None),
    "expired": ([_expired], "trash_retention_expired"),
    "location_taken": ([_location_taken], "location_already_exists"),
    "expired_and_location_taken": ([_location_taken, _expired], "trash_retention_expired"),
}


@pytest.mark.parametrize("case", BLOCKERS)
def test_the_bin_list_and_the_restore_action_name_the_same_blocker(
    monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    system, _ = make_plant(monkeypatch)
    client = Client()
    client.force_login(system.member)
    tenant = f"/api/tenants/{system.tenant.pk}/"
    assert client.delete(f"{tenant}regions/{system.region.pk}/").status_code == 204
    arrange, blocker = BLOCKERS[case]
    for step in arrange:
        step(system, monkeypatch)
    client.force_login(system.member)

    listed = client.get(f"{tenant}trash/regions/").json()["results"]
    restored = client.post(
        f"{tenant}regions/{system.region.pk}/restore/", {}, content_type="application/json"
    )

    assert [row["restore_blocker"] for row in listed] == [blocker]
    assert "can_restore" not in listed[0]
    answer = None if restored.status_code == 200 else restored.json()["code"]
    assert answer == blocker


def test_a_bin_listing_reads_every_blocker_in_one_query(monkeypatch: pytest.MonkeyPatch) -> None:
    system, _ = make_plant(monkeypatch)
    for region in (system.region, sibling(system)[0]):
        trash.trash_region(system.member, system.tenant.pk, region.pk)
    client = Client()
    client.force_login(system.member)

    with CaptureQueriesContext(connection) as queries:
        rows = client.get(f"/api/tenants/{system.tenant.pk}/trash/regions/").json()["results"]

    assert len(rows) == 2
    reads = [query["sql"] for query in queries if 'FROM "regions_region"' in query["sql"]]
    assert len(reads) == 2


def test_a_purge_logs_one_record_naming_what_it_removed_without_names_places_or_keys(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    system, plant = make_plant(monkeypatch)
    Region.objects.filter(pk=system.region.pk).update(
        name="Secret Valley", latitude=12.345678, longitude=98.765432
    )
    Plant.objects.filter(pk=plant.pk).update(name="Hidden Farm")
    trash.trash_region(system.member, system.tenant.pk, system.region.pk)
    caplog.set_level(logging.INFO, logger="pyracast.trash")

    assert trash.purge_expired(NOW + trash.RETENTION) == {"regions": 1, "plants": 1}

    records = [record for record in caplog.records if record.msg.startswith("trash_purged")]
    assert [record.getMessage() for record in records] == [
        f"trash_purged kind=region region_id={system.region.pk} tenant_id={system.tenant.pk} "
        f"deleted_at={NOW.isoformat()} plant_ids=[{plant.pk}] "
        f"bindings=[fixture:{system.account.pk}:site]"
    ]
    assert records[0].levelno == logging.INFO
    text = records[0].getMessage()
    for private in ("Secret Valley", "Hidden Farm", "12.345678", "98.765432", "customer"):
        assert private not in text


def test_a_purge_that_runs_out_of_budget_logs_nothing_until_the_region_is_gone(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    system, _ = make_plant(monkeypatch)
    trash.trash_region(system.member, system.tenant.pk, system.region.pk)
    caplog.set_level(logging.INFO, logger="pyracast.trash")
    monkeypatch.setattr("pyracast.trash.PURGE_STATEMENT_BUDGET", 1)

    trash.purge_expired(NOW + trash.RETENTION)

    assert Region.objects.filter(pk=system.region.pk).exists()
    assert not [record for record in caplog.records if record.msg.startswith("trash_purged")]


def test_a_plant_row_is_deleted_only_by_the_location_purge() -> None:
    deleting = []
    for path in SOURCE.rglob("*.py"):
        if "migrations" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for function in ast.walk(tree):
            if not isinstance(function, ast.FunctionDef):
                continue
            for call in ast.walk(function):
                if not isinstance(call, ast.Call):
                    continue
                text = ast.unparse(call)
                name = call.func.attr if isinstance(call.func, ast.Attribute) else ""
                name = call.func.id if isinstance(call.func, ast.Name) else name
                if name in {"delete", "_delete", "_raw_delete"} and "Plant.objects" in text:
                    deleting.append((path.relative_to(SOURCE).as_posix(), function.name))
    assert deleting == [("trash.py", "_purge_plant")]


def test_the_purge_deletes_plant_rows_only_while_it_holds_the_region_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, _ = make_plant(monkeypatch)
    trash.trash_region(system.member, system.tenant.pk, system.region.pk)

    with CaptureQueriesContext(connection) as queries:
        trash.purge_expired(NOW + trash.RETENTION)

    statements = [query["sql"] for query in queries]
    lock = next(
        index
        for index, sql in enumerate(statements)
        if sql.startswith('SELECT "regions_region"') and "FOR UPDATE" in sql
    )
    delete = next(
        index
        for index, sql in enumerate(statements)
        if sql.startswith('DELETE FROM "plants_plant"')
    )
    assert lock < delete
    assert not any(sql in {"COMMIT", "RELEASE SAVEPOINT"} for sql in statements[lock:delete])


def _trash_location(delivery: NotificationDelivery) -> None:
    region = delivery.schedule.region
    admin = Membership.objects.get(tenant_id=region.tenant_id).user
    trash.trash_region(admin, region.tenant_id, region.pk)


def test_a_delivery_trashed_before_its_transmission_marker_is_cancelled_not_uncertain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delivery = ready()
    sent: list[EmailMultiAlternatives] = []
    marker = jobs._mark_transmit

    def trash_before_marker(
        delivery_id: int, token: UUID, now: datetime, message: EmailMultiAlternatives
    ) -> bool:
        _trash_location(delivery)
        return marker(delivery_id, token, now, message)

    monkeypatch.setattr(jobs, "_mark_transmit", trash_before_marker)

    assert not process_notification(delivery.pk, SLOT, sender=sent.append)

    delivery.refresh_from_db()
    assert (delivery.status, delivery.last_error) == ("cancelled", "definition_trashed")
    assert (delivery.lease_token, delivery.lease_expires_at) == (None, None)
    assert sent == []


@pytest.mark.parametrize(
    ("outcome", "status", "error"),
    [
        ("accepted", "sent", ""),
        ("not_sent", "cancelled", "definition_trashed"),
        ("last_not_sent", "cancelled", "definition_trashed"),
        ("rejected", "cancelled", "definition_trashed"),
    ],
)
def test_a_delivery_trashed_while_transmitting_waits_for_its_late_outcome(
    outcome: str, status: str, error: str
) -> None:
    delivery = ready()
    if outcome == "last_not_sent":
        NotificationDelivery.objects.filter(pk=delivery.pk).update(attempts=7)
    seen: list[tuple[str, str, bool]] = []

    def trash_while_sending(message: EmailMultiAlternatives) -> None:
        _trash_location(delivery)
        row = NotificationDelivery.objects.get(pk=delivery.pk)
        seen.append((row.status, row.last_error, row.lease_token is not None))
        if outcome == "rejected":
            raise PermanentlyNotSentError("recipient refused")
        if outcome in {"not_sent", "last_not_sent"}:
            raise NotSentError("connection refused before DATA")

    process_notification(delivery.pk, SLOT, sender=trash_while_sending)

    assert seen == [("uncertain", "definition_trashed", True)]
    delivery.refresh_from_db()
    assert (delivery.status, delivery.last_error) == (status, error)
    assert (delivery.lease_token, delivery.lease_expires_at) == (None, None)
