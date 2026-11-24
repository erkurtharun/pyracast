import base64
import json
from collections.abc import Iterator
from datetime import timedelta
from email.message import EmailMessage as WireMessage
from typing import Any

import httpx
import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth.models import User
from django.core.mail import EmailMessage
from django.core.validators import validate_email
from django.test import Client, override_settings
from django.utils import timezone

from pyracast.accounts.models import Membership
from pyracast.mail.gmail import SEND_SCOPE, TOKEN_URL
from pyracast.mail.policy import wire_policy
from pyracast.notifications.jobs import process_notification
from pyracast.notifications.models import (
    DeliveryRecipient,
    NotificationDelivery,
    NotificationDeliveryError,
    NotificationDeliveryStatus,
    NotificationSettings,
    RecipientRole,
    ReminderRecipient,
    ReminderSchedule,
)
from pyracast.notifications.operations import invalid_recipient_region_ids
from pyracast.notifications.recipients import carries_exactly, serialized_headers
from pyracast.notifications.scheduler import enqueue_due
from pyracast.notifications.serializers import ReminderInput
from pyracast.regions.models import AuditEvent
from tests.support.domain import NOW, System, make_system
from tests.support.notification_groups import member
from tests.support.notifications import (
    SENDER,
    SLOT,
    chosen,
    confirmed_put,
    mailboxes,
    moved,
    parsed,
    smtp_wire,
    store,
    ties,
)

TO, CC, BCC = RecipientRole.TO, RecipientRole.CC, RecipientRole.BCC
ADMIN = "admin@example.com"
ACE, UPPER = "ops@xn--bcher-kva.de", "OPS@XN--BCHER-KVA.DE"
MIXED = "Ops.Team@Example.org"
DISTINCT = ("ops@xn--strae-oqa8r.de", "ops@xn--strasse-t2a.de")
ACCEPTED = [ACE, UPPER, MIXED, *DISTINCT]
STRASSE_ACE, STRASSE_CAPITAL = "ops@xn--strae-301b.de", "ops@stra\u1e9ee.de"
KELVIN = "t\u212ast@example.com"
DISPLAY_NAME = "Ops <ops@example.com>"
PARENTHESES = "ops@example.com\uff08x\uff09"
ESCAPED_SPACE, QUOTED_SPACE = '"a\\ b"@example.com', '"a b"@example.com'
LITERAL, FULL_WIDTH_LITERAL = "ops@[192.0.2.1]", "ops@\uff3b192.0.2.\uff10\uff11\uff3d"
OPENING, CLOSING, HIDDEN = "=?utf-8?q?alice@example.org", "bob?=@example.net", "alice@example.org"
LOWERED_ENCODED_WORD = "=?utf-16?b?YQ==?=@example.com"
KELVIN_DOMAIN = "ops@\u212aelvin.com"
REMOVED_SPECIALS = "!#$%&'*/=?^`{|}~"
INVALID = "Enter a valid email address."


def long_unicode_address() -> str:
    """At most 254 characters, but more than 254 octets once its domain is in ACE form."""
    domain = ".".join(["\u00e4" * 57] * 4) + ".de"
    return "o" * (253 - len(domain)) + "@" + domain


REFUSED = {
    "soft hyphen": "ops@soft\u00admail.de",
    "quoted local part": '"ops"@example.com',
    "unicode domain": "ops@b\u00fccher.de",
    "full-width letters": "ops@\uff46\uff55\uff4c\uff4c.de",
    "full-width letter beside a unicode one": "ops@\uff42\u00fccher.de",
    "replacement character": "ops@a\ufffdb.de",
    "full-width parentheses": PARENTHESES,
    "full-width comma": "ops@exa\uff0cmple.com",
    "ideographic full stop": "ops@example.com\u3002",
    "capital sharp s": STRASSE_CAPITAL,
    "georgian capitals": "ops@\u1c90\u1c91.ge",
    "kelvin sign": KELVIN,
    "long s": "t\u017fst@example.com",
    "dotted capital i": "t\u0130st@example.com",
    "dotless i": "t\u0131st@example.com",
    "254 characters, longer once encoded": long_unicode_address(),
    "display name": DISPLAY_NAME,
    "escaped space": ESCAPED_SPACE,
    "quoted space": QUOTED_SPACE,
    "full-width address literal": FULL_WIDTH_LITERAL,
    "address literal": LITERAL,
    "tagged ipv6 literal": "ops@[IPv6:2001:db8::1]",
    "expanded tagged ipv6 literal": "ops@[IPv6:2001:0db8:0000:0000:0000:0000:0000:0001]",
    "untagged ipv6 literal": "ops@[2001:db8::1]",
    "full-width underscore": "x@ex\uff3fample.com",
    "underscore": "x@ex_ample.com",
    "quoted backslash and tab": '"a\\\tb"@example.com',
    "65-octet local part": "a" * 65 + "@example.com",
    **{
        f"{special!r} inside a local part": f"a{special}b@example.com"
        for special in REMOVED_SPECIALS
    },
    "an encoded word opening": OPENING,
    "an encoded word closing": CLOSING,
    "an encoded word lower-casing changes": LOWERED_ENCODED_WORD,
}
REFUSED_IDS = list(REFUSED)


@pytest.mark.parametrize("address", ACCEPTED)
@pytest.mark.parametrize("eight_bit", [True, False])
def test_a_plain_address_serializes_in_every_header_under_both_wire_policies(
    address: str, eight_bit: bool
) -> None:
    message = WireMessage(policy=wire_policy(eight_bit=eight_bit))
    for name in ("To", "Cc", "Bcc"):
        message[name] = address

    serialized = parsed(message.as_bytes())

    assert [mailboxes(serialized, name) for name in ("To", "Cc", "Bcc")] == [[address]] * 3


pytestdb = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def saved_at_now(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(timezone, "now", lambda: NOW)


def workspace() -> System:
    system = make_system(approved=True)
    system.member.email = ADMIN
    system.member.save(update_fields=["email"])
    EmailAddress.objects.create(user=system.member, email=ADMIN, verified=True, primary=True)
    NotificationSettings.objects.update_or_create(pk=1, defaults={"max_recipients": 30})
    return system


def person(system: System, address: str, *, also: str = "", primary_verified: bool = True) -> User:
    """A member whose primary address was stored by other means than the product."""
    user = User.objects.create(username=f"held-{User.objects.count()}", email=address)
    Membership.objects.create(tenant=system.tenant, user=user)
    EmailAddress.objects.create(user=user, email=address, verified=primary_verified, primary=True)
    if also:
        EmailAddress.objects.create(user=user, email=also, verified=True, primary=False)
    return user


def membership(user: User) -> int:
    return Membership.objects.get(user=user).pk


def url(system: System, suffix: str = "") -> str:
    return f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/reminders/{suffix}"


def revision(system: System) -> int:
    return (
        ReminderSchedule.objects.filter(region=system.region)
        .values_list("revision", flat=True)
        .first()
        or 0
    )


def body_of(system: System, **changes: Any) -> dict[str, Any]:
    return {
        "enabled": True,
        "include_workspace_members": True,
        "language": "en",
        "recipients": [],
        "times": ["10:00"],
        "expected_revision": revision(system),
        "hide_recipients": False,
    } | changes


def request(system: System, suffix: str = "", **changes: Any) -> Any:
    client = Client()
    client.force_login(system.member)
    if not suffix:
        return confirmed_put(client, url(system), body_of(system, **changes))
    return client.post(
        url(system, suffix), body_of(system, **changes), content_type="application/json"
    )


def send_on(day: int = 0) -> int:
    slot = SLOT + timedelta(days=day)
    delivery_id = enqueue_due(slot)[-1]
    process_notification(delivery_id, slot)
    return delivery_id


def refused_fields(response: Any) -> list[str]:
    assert response.status_code == 400, response.content
    return sorted({name.split(".")[0] for name in response.json()["fields"]})


def field_errors(response: Any) -> dict[str, str]:
    assert response.status_code == 400, response.content
    return dict(response.json()["fields"])


def error_codes(errors: Any) -> set[str]:
    if isinstance(errors, dict):
        return set().union(*(error_codes(value) for value in errors.values()))
    if isinstance(errors, list):
        return set().union(*(error_codes(value) for value in errors))
    return {errors.code}


@pytestdb
@pytest.mark.parametrize(("spelling", "other"), [(ACE, UPPER), (MIXED, MIXED.lower())])
def test_two_case_spellings_of_one_address_are_refused_as_duplicates(
    spelling: str, other: str
) -> None:
    system = workspace()

    response = request(system, recipients=[spelling, other])

    assert response.status_code == 400, response.content
    assert (response.json()["code"], list(response.json()["fields"])) == (
        "duplicate_recipients",
        ["recipients"],
    )


@pytestdb
def test_two_distinct_ace_domains_are_two_recipients() -> None:
    system = workspace()

    response = request(system, recipients=list(DISTINCT))

    assert response.status_code == 200, response.content
    assert response.json()["addresses"]["recipients"] == list(DISTINCT)


@pytestdb
@pytest.mark.parametrize("address", list(REFUSED.values()), ids=REFUSED_IDS)
def test_an_address_that_is_not_plain_is_refused_at_its_field_by_the_preview_and_the_save(
    address: str,
) -> None:
    system = workspace()

    answers = [request(system, suffix, recipients=[address]) for suffix in ("preview/", "")]

    assert [refused_fields(response) for response in answers] == [["recipients"]] * 2
    assert not ReminderSchedule.objects.exists()
    assert not AuditEvent.objects.filter(action="reminder_schedule_updated").exists()
    serializer = ReminderInput(data=body_of(system, recipients=[address]))
    assert not serializer.is_valid()
    assert list(serializer.errors) == ["recipients"]
    assert error_codes(serializer.errors["recipients"]) == {validate_email.code}


@pytestdb
@pytest.mark.parametrize("address", [LITERAL, FULL_WIDTH_LITERAL, ESCAPED_SPACE, PARENTHESES])
def test_a_role_entry_naming_an_address_that_is_not_plain_is_refused_at_its_field(
    address: str,
) -> None:
    system = workspace()
    entries = {
        "address_roles": [{"address": address, "role": "to", "memberships": []}],
        "member_roles": [
            {"memberships": [membership(system.member)], "address": address, "role": "cc"}
        ],
    }

    answers = {
        (name, suffix): request(system, suffix, recipients=["ops@example.com"], **{name: entry})
        for name, entry in entries.items()
        for suffix in ("preview/", "")
    }

    assert {place: field_errors(response) for place, response in answers.items()} == {
        (name, suffix): {f"{name}.0.address": INVALID} for name, suffix in answers
    }
    assert not ReminderSchedule.objects.exists()
    for name, entry in entries.items():
        serializer = ReminderInput(
            data=body_of(system, recipients=["ops@example.com"], **{name: entry})
        )
        assert not serializer.is_valid()
        assert list(serializer.errors) == [name]
        assert list(serializer.errors[name]) == [0]
        assert list(serializer.errors[name][0]) == ["address"]
        assert error_codes(serializer.errors[name][0]["address"]) == {validate_email.code}


def typed_roles() -> list[tuple[str, RecipientRole]]:
    return [
        (UPPER, TO),
        (MIXED, CC),
        ("ops@xn--mnchen-3ya.de", BCC),
        (DISTINCT[0], TO),
        (DISTINCT[1], CC),
        ("First.Last+tag@example.com", TO),
        ("ops_team-1@mail.example.net", CC),
        ("a.b_c+d-e@Example.ORG", BCC),
        ("x.y@xn--bcher-kva.de", BCC),
    ]


def typed_schedule(system: System) -> None:
    roles = typed_roles()
    response = request(
        system,
        include_workspace_members=False,
        recipients=[address for address, _ in roles],
        address_roles=[
            {"address": address, "role": role, "memberships": []} for address, role in roles
        ],
    )
    assert response.status_code == 200, response.content


def typed_as(role: RecipientRole) -> list[str]:
    return [address for address, given in typed_roles() if given == role]


@pytestdb
@pytest.mark.parametrize("eight_bit", [True, False])
def test_the_smtp_headers_and_envelope_carry_each_plain_address_as_typed(eight_bit: bool) -> None:
    system = workspace()
    typed_schedule(system)

    with smtp_wire() as script:
        if not eight_bit:
            script.extensions = ["SIZE 10000000"]
        send_on()

    (transaction,) = script.transactions
    assert transaction.mail_from.upper().endswith(" BODY=8BITMIME") == eight_bit
    message = parsed(transaction.data)
    assert message["To"] == ", ".join(typed_as(TO))
    assert message["Cc"] == ", ".join(typed_as(CC))
    assert message["Bcc"] is None
    assert (mailboxes(message, "To"), mailboxes(message, "Cc")) == (typed_as(TO), typed_as(CC))
    assert transaction.recipients == typed_as(TO) + typed_as(CC) + typed_as(BCC)
    assert transaction.data.isascii()


@pytest.fixture
def gmail_requests(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[bytes]]:
    sent: list[bytes] = []

    def respond(http: httpx.Request) -> httpx.Response:
        if str(http.url) == TOKEN_URL:
            return httpx.Response(
                200, json={"access_token": "access", "token_type": "Bearer", "scope": SEND_SCOPE}
            )
        sent.append(base64.urlsafe_b64decode(json.loads(http.content)["raw"]))
        return httpx.Response(200, json={"id": "accepted"})

    client = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs)
    )
    options = {"client_id": "client", "client_secret": "secret", "refresh_token": "refresh"}
    with override_settings(
        DEFAULT_FROM_EMAIL=SENDER,
        MAILERS={"default": {"BACKEND": "pyracast.mail.gmail.EmailBackend", "OPTIONS": options}},
    ):
        yield sent


@pytestdb
def test_the_gmail_request_carries_each_plain_address_as_typed(
    gmail_requests: list[bytes],
) -> None:
    system = workspace()
    typed_schedule(system)

    delivery_id = send_on()

    assert NotificationDelivery.objects.get(pk=delivery_id).status == "sent"
    (raw,) = gmail_requests
    message = parsed(raw)
    assert message["To"] == ", ".join(typed_as(TO))
    assert message["Cc"] == ", ".join(typed_as(CC))
    assert message["Bcc"] == ", ".join(typed_as(BCC))
    assert [mailboxes(message, name) for name in ("To", "Cc", "Bcc")] == [
        typed_as(TO),
        typed_as(CC),
        typed_as(BCC),
    ]


@pytestdb
def test_an_all_hidden_gmail_message_carries_every_plain_address_in_its_hidden_list(
    gmail_requests: list[bytes],
) -> None:
    system = workspace()
    addresses = [address for address, _ in typed_roles()]
    saved = request(
        system, include_workspace_members=False, recipients=addresses, hide_recipients=True
    )
    assert saved.status_code == 200, saved.content

    delivery_id = send_on()

    assert NotificationDelivery.objects.get(pk=delivery_id).status == "sent"
    (raw,) = gmail_requests
    message = parsed(raw)
    assert (message["To"], message["Cc"], message["Bcc"]) == (None, None, ", ".join(addresses))
    assert [mailboxes(message, name) for name in ("To", "Cc", "Bcc")] == [[], [], addresses]


def composed(to: list[str], cc: list[str], bcc: list[str]) -> EmailMessage:
    return EmailMessage("Reminder", "Body", SENDER, to=to, cc=cc, bcc=bcc)


def test_two_addresses_read_back_as_one_encoded_word_differ_from_their_placement() -> None:
    message = composed([OPENING, CLOSING], [], [HIDDEN])

    read = serialized_headers(message)

    rewritten = ["alice@example.org", "bob@example.net"]
    assert read == {
        "smtp 7bit": (rewritten, [], []),
        "smtp 8bit": (rewritten, [], []),
        "gmail": (rewritten, [], [HIDDEN]),
    }
    assert not carries_exactly(message, ([OPENING, CLOSING], [], [HIDDEN]))


def test_an_all_hidden_gmail_message_of_such_addresses_differs_in_its_hidden_list() -> None:
    message = composed([], [], [OPENING, CLOSING])

    read = serialized_headers(message)

    assert read["gmail"] == ([], [], ["alice@example.org", "bob@example.net"])
    assert read["smtp 7bit"] == read["smtp 8bit"] == ([], [], [])
    assert not carries_exactly(message, ([], [], [OPENING, CLOSING]))


def test_several_plain_addresses_read_back_exactly_in_every_serialization() -> None:
    to, cc, bcc = (typed_as(role) for role in (TO, CC, BCC))
    message = composed(to, cc, bcc)

    assert serialized_headers(message) == {
        "smtp 7bit": (to, cc, []),
        "smtp 8bit": (to, cc, []),
        "gmail": (to, cc, bcc),
    }
    assert carries_exactly(message, (to, cc, bcc))
    assert carries_exactly(composed([], [], bcc + to), ([], [], bcc + to))


@pytestdb
def test_a_visible_choice_does_not_pass_to_a_distinct_ace_domain() -> None:
    system = workspace()
    first, second = DISTINCT
    saved = request(
        system,
        include_workspace_members=False,
        recipients=[first],
        address_roles=[{"address": first, "role": "to", "memberships": []}],
    )
    assert saved.status_code == 200, saved.content

    replaced = request(system, include_workspace_members=False, recipients=[second])

    assert replaced.status_code == 200, replaced.content
    assert [(row["address"], row["role"]) for row in replaced.json()["addresses"]["added"]] == [
        (second, None)
    ]
    with smtp_wire() as script:
        send_on()
    message = parsed(script.transactions[0].data)
    assert (message["To"], message["Cc"]) == (None, None)
    assert (mailboxes(message, "To"), mailboxes(message, "Cc")) == ([], [])
    assert script.transactions[0].recipients == [second]


def skipped_as_invalid(delivery_id: int) -> bool:
    delivery = NotificationDelivery.objects.get(pk=delivery_id)
    return (delivery.status, delivery.last_error) == (
        NotificationDeliveryStatus.SKIPPED,
        NotificationDeliveryError.INVALID_RECIPIENT,
    )


@pytestdb
@pytest.mark.parametrize("address", list(REFUSED.values()), ids=REFUSED_IDS)
def test_a_member_address_that_is_not_plain_skips_the_delivery_before_any_transport(
    gmail_requests: list[bytes], address: str
) -> None:
    system = workspace()
    person(system, address)
    assert request(system).status_code == 200

    with smtp_wire() as script:
        over_smtp = send_on()
    over_gmail = send_on(1)

    assert (script.transactions, gmail_requests) == ([], [])
    assert skipped_as_invalid(over_smtp)
    assert skipped_as_invalid(over_gmail)
    assert not DeliveryRecipient.objects.exists()
    assert invalid_recipient_region_ids() == {system.region.pk}


@pytestdb
@pytest.mark.parametrize("address", list(REFUSED.values()), ids=REFUSED_IDS)
def test_a_member_address_that_is_not_plain_skips_a_delivery_sending_it_only_in_bcc(
    address: str,
) -> None:
    system = workspace()
    person(system, address)
    assert request(system, hide_recipients=True).status_code == 200

    with smtp_wire() as script:
        delivery_id = send_on()

    assert script.transactions == []
    assert skipped_as_invalid(delivery_id)
    assert not DeliveryRecipient.objects.exists()
    assert invalid_recipient_region_ids() == {system.region.pk}


def answer(system: System) -> Any:
    client = Client()
    client.force_login(system.member)
    response = client.get(url(system))
    assert response.status_code == 200, response.content
    return response.json()


def sent_headers(day: int) -> tuple[list[str], list[str], list[str]]:
    with smtp_wire() as script:
        send_on(day)
    (transaction,) = script.transactions
    message = parsed(transaction.data)
    return mailboxes(message, "To"), mailboxes(message, "Cc"), transaction.recipients


def skipped_send(day: int, system: System) -> list[str]:
    """Skipped before any transport and reported; the rows the slot froze, never marked."""
    with smtp_wire() as script:
        delivery_id = send_on(day)
    assert script.transactions == []
    assert skipped_as_invalid(delivery_id)
    assert NotificationDelivery.objects.get(pk=delivery_id).transmit_started_at is None
    assert invalid_recipient_region_ids() == {system.region.pk}
    return list(
        DeliveryRecipient.objects.filter(delivery_id=delivery_id)
        .order_by("pk")
        .values_list("address", flat=True)
    )


@pytestdb
@pytest.mark.parametrize("include", [True, False])
def test_a_member_address_the_validator_refuses_skips_a_list_holding_its_mailbox_typed(
    gmail_requests: list[bytes], include: bool
) -> None:
    system = workspace()
    person(system, DISPLAY_NAME)
    preview = request(
        system, "preview/", include_workspace_members=include, recipients=["ops@example.com"]
    )
    (shown,) = preview.json()["addresses"]["added"]
    saved = request(
        system,
        include_workspace_members=include,
        recipients=["ops@example.com"],
        address_roles=[
            {"address": "ops@example.com", "role": "to", "memberships": shown["memberships"]}
        ],
    )
    assert saved.status_code == 200, saved.content

    assert skipped_send(0, system) == ([] if include else ["ops@example.com"])
    over_gmail = send_on(1)

    assert gmail_requests == []
    assert skipped_as_invalid(over_gmail)
    assert answer(system)["state"] == "invalid_recipient"


def stored_mode(system: System) -> tuple[int, bool, bool]:
    schedule = ReminderSchedule.objects.get(region=system.region)
    return schedule.revision, schedule.hide_recipients, schedule.hidden_by_departure


@pytestdb
def test_a_hidden_member_moving_to_an_address_that_is_not_plain_skips_the_list_until_back() -> None:
    system = workspace()
    holder = member(system, "holder@example.com")
    saved = request(
        system,
        include_workspace_members=False,
        recipients=[STRASSE_ACE],
        address_roles=[{"address": STRASSE_ACE, "role": "to", "memberships": []}],
        member_roles=[
            {"memberships": [membership(holder)], "address": "holder@example.com", "role": "bcc"}
        ],
    )
    assert saved.status_code == 200, saved.content
    moved(holder, STRASSE_ACE)
    assert sent_headers(0) == ([], [], [STRASSE_ACE])
    before = stored_mode(system)

    moved(holder, STRASSE_CAPITAL)

    assert skipped_send(1, system) == [STRASSE_ACE]
    shown = answer(system)
    assert shown["state"] == "invalid_recipient"
    assert [
        (row["address"], row["role"], row["placement"]) for row in shown["addresses"]["added"]
    ] == [(STRASSE_ACE, "to", "bcc")]
    assert shown["hide_recipients"] is False
    assert stored_mode(system) == before
    assert before[1:] == (False, False)
    moved(holder, STRASSE_ACE)
    assert sent_headers(2) == ([], [], [STRASSE_ACE])
    added = answer(system)["addresses"]["added"]
    assert [(row["address"], row["role"], row["placement"]) for row in added] == [
        (STRASSE_ACE, "bcc", "bcc")
    ]
    assert stored_mode(system) == before


@pytestdb
@pytest.mark.parametrize("hide", [True, False])
def test_an_account_address_that_is_not_plain_skips_only_a_list_that_reads_people(
    gmail_requests: list[bytes], hide: bool
) -> None:
    system = workspace()
    person(system, KELVIN)
    saved = request(
        system,
        include_workspace_members=False,
        recipients=["outside@example.com"],
        hide_recipients=hide,
    )
    assert saved.status_code == 200, saved.content

    with smtp_wire() as script:
        delivery_id = send_on()
    over_gmail = send_on(1)

    if hide:
        (transaction,) = script.transactions
        message = parsed(transaction.data)
        assert (message["To"], message["Cc"], message["Bcc"]) == (None, None, None)
        assert (mailboxes(message, "To"), mailboxes(message, "Cc")) == ([], [])
        assert transaction.recipients == ["outside@example.com"]
        (raw,) = gmail_requests
        assert [mailboxes(parsed(raw), name) for name in ("To", "Cc", "Bcc")] == [
            [],
            [],
            ["outside@example.com"],
        ]
        assert answer(system)["state"] == "sending"
        assert invalid_recipient_region_ids() == set()
    else:
        assert (script.transactions, gmail_requests) == ([], [])
        assert skipped_as_invalid(delivery_id)
        assert skipped_as_invalid(over_gmail)
        assert answer(system)["state"] == "invalid_recipient"
        assert invalid_recipient_region_ids() == {system.region.pk}


@pytestdb
def test_a_member_holding_a_quoted_local_part_skips_the_list_until_they_hold_a_plain_address() -> (
    None
):
    system = workspace()
    holder = person(system, QUOTED_SPACE)
    assert request(system).status_code == 200
    typed = request(
        system,
        recipients=[ESCAPED_SPACE],
        address_roles=[{"address": ESCAPED_SPACE, "role": "bcc", "memberships": []}],
    )
    assert field_errors(typed) == {"recipients": INVALID, "address_roles.0.address": INVALID}
    moved(holder, ESCAPED_SPACE)

    assert skipped_send(0, system) == []

    moved(holder, "new@example.com")
    assert sent_headers(1) == ([ADMIN], ["new@example.com"], [ADMIN, "new@example.com"])


@pytestdb
@pytest.mark.parametrize("include", [True, False])
def test_a_member_holding_an_address_literal_skips_the_list_until_they_hold_a_plain_address(
    include: bool,
) -> None:
    system = workspace()
    holder = person(system, LITERAL)
    body: dict[str, Any] = {"include_workspace_members": include}
    assert request(system, **body, recipients=["outside@example.com"]).status_code == 200
    chosen(ReminderSchedule.objects.get(region=system.region), holder, BCC)
    typed = request(
        system,
        **body,
        recipients=["outside@example.com", FULL_WIDTH_LITERAL],
        address_roles=[{"address": FULL_WIDTH_LITERAL, "role": "to", "memberships": []}],
    )
    assert field_errors(typed) == {"recipients": INVALID, "address_roles.0.address": INVALID}

    assert skipped_send(0, system) == ([] if include else ["outside@example.com"])

    moved(holder, "ops@example.com")
    assert sent_headers(1) == (
        ([ADMIN], [], [ADMIN, "outside@example.com", "ops@example.com"])
        if include
        else ([], [], ["outside@example.com"])
    )


@pytestdb
@pytest.mark.parametrize(
    ("primary", "other"),
    [("tsst@example.com", "t\u017fst@example.com"), ("t\u017fst@example.com", "tsst@example.com")],
    ids=["plain primary", "plain verified address"],
)
def test_a_member_with_a_verified_address_that_is_not_plain_skips_the_list(
    primary: str, other: str
) -> None:
    system = workspace()
    person(system, primary, also=other)
    assert request(system).status_code == 200

    assert skipped_send(0, system) == ([ADMIN, primary] if primary.isascii() else [])


@pytestdb
@pytest.mark.parametrize(
    ("primary", "other"),
    [("tsst@example.com", "t\u017fst@example.com"), ("t\u017fst@example.com", "tsst@example.com")],
    ids=["plain primary", "plain verified address"],
)
def test_a_member_whose_only_verified_address_is_the_other_spelling_skips_the_list(
    gmail_requests: list[bytes], primary: str, other: str
) -> None:
    system = workspace()
    person(system, primary, also=other, primary_verified=False)
    assert request(system).status_code == 200

    assert skipped_send(0, system) == [ADMIN]
    over_gmail = send_on(1)

    assert gmail_requests == []
    assert skipped_as_invalid(over_gmail)


@pytestdb
def test_a_member_address_that_is_not_plain_ties_no_typed_look_alike() -> None:
    system = workspace()
    person(system, STRASSE_CAPITAL)
    person(system, KELVIN_DOMAIN)

    saved = request(
        system,
        include_workspace_members=False,
        recipients=["ops@strasse.de", "ops@kelvin.com"],
        hide_recipients=True,
    )

    assert saved.status_code == 200, saved.content
    assert ties(ReminderSchedule.objects.get(region=system.region)) == {}
    assert [row["memberships"] for row in saved.json()["addresses"]["added"]] == [[], []]


@pytestdb
@pytest.mark.parametrize(
    ("stored", "plain"), [(STRASSE_CAPITAL, "ops@strasse.de"), (KELVIN_DOMAIN, "ops@kelvin.com")]
)
def test_a_typed_row_that_is_not_plain_passes_no_choice_to_its_plain_look_alike(
    stored: str, plain: str
) -> None:
    system = workspace()
    first = request(system, include_workspace_members=False, recipients=["outside@example.com"])
    assert first.status_code == 200, first.content
    store(ReminderSchedule.objects.get(region=system.region), [stored])
    ReminderRecipient.objects.update(role=TO)

    replaced = request(system, include_workspace_members=False, recipients=[plain])

    assert replaced.status_code == 200, replaced.content
    assert list(ReminderRecipient.objects.values_list("address", "role", "role_members")) == [
        (plain, "", [])
    ]


@pytestdb
def test_a_typed_row_stored_by_other_means_that_is_not_plain_skips_the_send() -> None:
    system = workspace()
    assert request(system).status_code == 200
    store(ReminderSchedule.objects.get(region=system.region), [PARENTHESES])
    ReminderRecipient.objects.update(role=TO)

    assert skipped_send(0, system) == []


def mixed_request() -> dict[str, Any]:
    entry = {"address": "ops@example.com", "role": "to", "memberships": []}
    return {"recipients": ["ops@example.com", PARENTHESES], "address_roles": [entry, entry]}


@pytestdb
def test_a_recipient_that_is_not_plain_is_refused_before_the_role_entries_are_checked() -> None:
    system = workspace()

    answers = [request(system, suffix, **mixed_request()) for suffix in ("preview/", "")]

    for response in answers:
        assert response.status_code == 400, response.content
        assert list(response.json()["fields"]) == ["recipients"]
    assert not ReminderSchedule.objects.exists()
    serializer = ReminderInput(
        data=body_of(system, include_workspace_members=False) | mixed_request()
    )
    assert not serializer.is_valid()
    assert list(serializer.errors) == ["recipients"]
    assert [error.code for error in serializer.errors["recipients"]] == [validate_email.code]


def typed_by_other_means(system: System) -> int:
    first = request(system, include_workspace_members=False, recipients=["outside@example.com"])
    assert first.status_code == 200, first.content
    store(ReminderSchedule.objects.get(region=system.region), [OPENING, CLOSING, HIDDEN])
    ReminderRecipient.objects.filter(address__in=[OPENING, CLOSING]).update(role=TO)
    ReminderRecipient.objects.filter(address=HIDDEN).update(role=BCC)
    return enqueue_due(SLOT)[-1]


def frozen_by_other_means(system: System, frozen: dict[str, str]) -> int:
    """Queued for plain current addresses; then the frozen rows' texts are rewritten in place."""
    saved = request(
        system,
        include_workspace_members=False,
        recipients=list(frozen),
        hide_recipients=True,
    )
    assert saved.status_code == 200, saved.content
    delivery_id = enqueue_due(SLOT)[-1]
    for key, text in frozen.items():
        DeliveryRecipient.objects.filter(delivery_id=delivery_id, key=key).update(address=text)
    return delivery_id


V1_FROZEN = {"a@example.org": OPENING, "b@example.net": CLOSING, HIDDEN: HIDDEN}
SHARP_S_FROZEN = {"ops@strasse.de": STRASSE_CAPITAL}


@pytestdb
@pytest.mark.parametrize("transport", ["smtp", "gmail"])
@pytest.mark.parametrize(
    "queued",
    [
        pytest.param(typed_by_other_means, id="typed rows"),
        pytest.param(lambda system: frozen_by_other_means(system, V1_FROZEN), id="frozen rows"),
        pytest.param(
            lambda system: frozen_by_other_means(system, SHARP_S_FROZEN),
            id="frozen sharp s beside its plain key",
        ),
    ],
)
def test_a_list_whose_texts_are_not_plain_is_skipped_before_the_transmit_marker(
    gmail_requests: list[bytes], transport: str, queued: Any
) -> None:
    system = workspace()
    delivery_id = queued(system)
    before = list(
        DeliveryRecipient.objects.filter(delivery_id=delivery_id)
        .order_by("pk")
        .values_list("address", "role")
    )

    with smtp_wire() as script:
        if transport == "smtp":
            process_notification(delivery_id, SLOT)
    if transport == "gmail":
        process_notification(delivery_id, SLOT)

    assert (script.transactions, gmail_requests) == ([], [])
    assert skipped_as_invalid(delivery_id)
    assert NotificationDelivery.objects.get(pk=delivery_id).transmit_started_at is None
    assert set(role for _, role in before) <= {BCC}
    assert (
        list(
            DeliveryRecipient.objects.filter(delivery_id=delivery_id)
            .order_by("pk")
            .values_list("address", "role")
        )
        == before
    )


@pytestdb
def test_a_queued_delivery_reaches_its_frozen_spelling_after_the_list_changes_its_case() -> None:
    system = workspace()
    first = request(system, include_workspace_members=False, recipients=[UPPER])
    assert first.status_code == 200, first.content
    (queued,) = enqueue_due(SLOT)
    respelled = request(system, include_workspace_members=False, recipients=[ACE])
    assert respelled.status_code == 200, respelled.content

    with smtp_wire() as script:
        process_notification(queued, SLOT)

    (transaction,) = script.transactions
    message = parsed(transaction.data)
    assert (mailboxes(message, "To"), mailboxes(message, "Cc")) == ([], [])
    assert transaction.recipients == [UPPER]


@pytestdb
def test_a_typed_case_spelling_of_a_members_address_is_one_recipient_counted_once() -> None:
    system = workspace()
    member(system, ACE)

    response = request(system, recipients=[UPPER])

    assert response.status_code == 200, response.content
    event = AuditEvent.objects.get(action="reminder_schedule_updated")
    assert (event.details["visible_recipients"], event.details["visible_outside"]) == (2, 0)
    NotificationSettings.objects.filter(pk=1).update(max_recipients=2)
    with smtp_wire() as script:
        delivery_id = send_on()
    (transaction,) = script.transactions
    message = parsed(transaction.data)
    assert (mailboxes(message, "To"), mailboxes(message, "Cc")) == ([ADMIN], [UPPER])
    assert transaction.recipients == [ADMIN, UPPER]
    assert list(
        DeliveryRecipient.objects.filter(delivery_id=delivery_id)
        .order_by("pk")
        .values_list("address", "role")
    ) == [(UPPER, CC), (ADMIN, TO)]
