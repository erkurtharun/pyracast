from unittest.mock import MagicMock

import psycopg
import pytest
from django.core.management import CommandError, call_command
from django.db import connection

from pyracast.forecasts.management.commands import dispatch as command
from pyracast.forecasts.models import CollectionJob, DispatcherStatus

pytestmark = pytest.mark.django_db(transaction=True)


def test_dispatch_reconnects_after_its_postgresql_session_is_terminated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = MagicMock()
    monkeypatch.setattr(command, "make_broker", lambda: broker)
    for factory in (
        "make_actor",
        "make_calibration_actor",
        "make_notification_actor",
        "make_outbound_mail_actor",
        "make_epias_actor",
    ):
        monkeypatch.setattr(command, factory, MagicMock())
    for operation in (
        "dispatch_collections",
        "dispatch_epias",
        "purge_expired",
        "purge_finished_mail",
    ):
        monkeypatch.setattr(command, operation, MagicMock())
    sessions: list[int] = []

    def interrupt_notifications(*_: object) -> None:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_backend_pid()")
            sessions.append(cursor.fetchone()[0])
        with (
            psycopg.connect(**connection.get_connection_params(), autocommit=True) as controller,
            controller.cursor() as cursor,
        ):
            cursor.execute("SELECT pg_terminate_backend(%s)", (sessions[0],))
            assert cursor.fetchone() == (True,)
        CollectionJob.objects.count()

    def next_phase(_: object) -> None:
        assert CollectionJob.objects.count() == 0
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_backend_pid()")
            sessions.append(cursor.fetchone()[0])

    monkeypatch.setattr(command, "dispatch_notifications", interrupt_notifications)
    monkeypatch.setattr(command, "dispatch_calibrations", next_phase)
    with pytest.raises(CommandError, match="notifications"):
        call_command("dispatch")
    assert len(sessions) == 2
    assert sessions[0] != sessions[1]
    failed = DispatcherStatus.objects.get(phase="notifications")
    assert failed.last_error == "OperationalError"
    assert failed.succeeded_at is None
    assert DispatcherStatus.objects.get(phase="calibration").succeeded_at is not None
    broker.close.assert_called_once()
