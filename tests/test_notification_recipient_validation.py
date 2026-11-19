from unittest.mock import MagicMock, call

import pytest
from django.core.exceptions import ValidationError
from django.core.validators import validate_email

from pyracast.background import failures
from pyracast.background.failures import ItemsFailedError
from pyracast.notifications import runtime
from pyracast.notifications.operations import configure_reminders, validate_recipient
from pyracast.notifications.serializers import ReminderInput
from tests.support.domain import NOW
from tests.support.notification_recipient_validation import recipient
from tests.support.notifications import configured


@pytest.mark.parametrize("length", [255, 320])
def test_api_rejects_valid_email_addresses_that_do_not_fit_delivery_storage(length: int) -> None:
    address = recipient(length)
    assert len(address) == length
    validate_email(address)
    serializer = ReminderInput(
        data={
            "enabled": True,
            "include_workspace_members": False,
            "recipients": [address],
            "times": ["10:00"],
            "language": "en",
        }
    )
    assert not serializer.is_valid()
    assert "recipients" in serializer.errors


@pytest.mark.parametrize("length", [255, 320])
def test_application_rejects_valid_email_addresses_that_do_not_fit_delivery_storage(
    length: int,
) -> None:
    address = recipient(length)
    validate_email(address)
    with pytest.raises(ValidationError, match="254"):
        validate_recipient(address)


@pytest.mark.django_db(transaction=True)
def test_exact_delivery_storage_limit_is_accepted_at_both_boundaries() -> None:
    address = recipient(254)
    padded = f" {address} "
    serializer = ReminderInput(
        data={
            "enabled": True,
            "include_workspace_members": False,
            "recipients": [padded],
            "times": ["10:00"],
            "language": "en",
        }
    )
    assert serializer.is_valid(), serializer.errors
    assert serializer.validated_data["recipients"] == [address]
    system, _ = configured()
    schedule = configure_reminders(
        system.member,
        system.tenant.pk,
        system.region.pk,
        enabled=True,
        recipients=[padded],
        times=["10:00"],
        language="en",
        now=NOW,
    )
    assert schedule.recipients == [address]


def test_pending_messages_are_dispatched_before_a_scheduling_failure_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schedule = MagicMock(side_effect=ItemsFailedError(1))
    pending = MagicMock(return_value=[11, 12])
    close_connections = MagicMock()
    actor = MagicMock()
    monkeypatch.setattr(runtime, "enqueue_due", schedule)
    monkeypatch.setattr(runtime, "pending_notification_ids", pending)
    monkeypatch.setattr(runtime, "publish_outbound_mail", MagicMock(return_value=0))
    monkeypatch.setattr(failures, "close_old_connections", close_connections)
    with pytest.raises(ItemsFailedError) as raised:
        runtime.dispatch_notifications(actor, MagicMock())
    assert raised.value.published == 2
    assert schedule.call_args == pending.call_args
    assert actor.send.call_args_list == [call(11), call(12)]
    close_connections.assert_called_once()


def test_a_publish_failure_does_not_replace_the_scheduling_failure(
    monkeypatch: pytest.MonkeyPatch, pyracast_logs: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(runtime, "enqueue_due", MagicMock(side_effect=LookupError("scheduling")))
    monkeypatch.setattr(runtime, "pending_notification_ids", MagicMock(return_value=[11]))
    actor = MagicMock()
    actor.send.side_effect = ConnectionError("private-broker-url")
    with pytest.raises(LookupError, match="scheduling"):
        runtime.dispatch_notifications(actor, MagicMock())
    assert "error_type=ConnectionError" in pyracast_logs.text
    assert "private-broker-url" not in pyracast_logs.text
