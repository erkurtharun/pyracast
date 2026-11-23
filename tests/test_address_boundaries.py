import pytest
from django.core.mail import EmailMessage
from django.core.management.base import CommandError

from pyracast.accounts.addresses import is_plain
from pyracast.accounts.management.operator import INVALID_ADDRESS, lookup_address
from pyracast.notifications import recipients
from pyracast.notifications.models import RecipientRole


def test_an_address_with_a_one_letter_top_level_label_is_not_plain() -> None:
    assert not is_plain("ops@example.c")


def test_the_operator_lookup_refuses_an_address_over_254_octets() -> None:
    address = f"{'a' * 64}@{'b' * 63}.{'c' * 63}.{'d' * 63}.com"

    assert len(address) > 254
    with pytest.raises(CommandError, match=INVALID_ADDRESS):
        lookup_address(address)


def test_headers_keep_the_first_placement_of_an_address_in_another_case() -> None:
    placed = recipients.headers(
        ["ops@example.com", "OPS@example.com", "b@example.com"],
        [RecipientRole.TO, RecipientRole.CC, RecipientRole.BCC],
    )

    assert placed == (["ops@example.com"], [], ["b@example.com"])


def test_a_repeated_header_reads_back_as_none() -> None:
    raw = b"To: a@example.com\r\nTo: b@example.com\r\nCc: c@example.com\r\n\r\n"

    assert recipients._lists(raw) == (None, ["c@example.com"], [])


def test_a_message_whose_envelope_differs_from_the_placed_headers_is_not_exact() -> None:
    message = EmailMessage(to=["a@example.com"])

    assert not recipients.carries_exactly(message, (["b@example.com"], [], []))
