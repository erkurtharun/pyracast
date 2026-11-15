from unittest.mock import MagicMock, call

import pytest
from django.core.exceptions import ValidationError
from django.core.validators import validate_email

from pyracast.notifications import runtime
from pyracast.notifications.operations import normalize_recipients
from pyracast.notifications.scheduler import NotificationSchedulingError
from pyracast.notifications.serializers import ReminderInput


def recipient(length: int) -> str:
    return "a" * (length - 191) + "@" + "b" * 63 + "." + "c" * 63 + "." + "d" * 62


@pytest.mark.parametrize("length", [255, 320])
def test_api_rejects_valid_email_addresses_that_do_not_fit_delivery_storage(length: int) -> None:
    address = recipient(length)
    assert len(address) == length
    validate_email(address)
    serializer = ReminderInput(
        data={"enabled": True, "recipients": [address], "times": ["10:00"], "language": "en"}
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
        normalize_recipients([address])


def test_exact_delivery_storage_limit_is_accepted_at_both_boundaries() -> None:
    address = recipient(254)
    serializer = ReminderInput(
        data={"enabled": True, "recipients": [address], "times": ["10:00"], "language": "en"}
    )
    assert serializer.is_valid(), serializer.errors
    assert normalize_recipients([" " + address + " "]) == [address]


def test_pending_messages_are_dispatched_before_a_scheduling_failure_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schedule = MagicMock(side_effect=NotificationSchedulingError("One schedule failed."))
    pending = MagicMock(return_value=[11, 12])
    close_connections = MagicMock()
    actor = MagicMock()
    monkeypatch.setattr(runtime, "enqueue_due", schedule)
    monkeypatch.setattr(runtime, "pending_notification_ids", pending)
    monkeypatch.setattr(runtime, "close_old_connections", close_connections)
    with pytest.raises(NotificationSchedulingError):
        runtime.dispatch_notifications(actor)
    assert schedule.call_args == pending.call_args
    assert actor.send.call_args_list == [call(11), call(12)]
    close_connections.assert_called_once()
