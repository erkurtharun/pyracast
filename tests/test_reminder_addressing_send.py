import base64
import json
from collections.abc import Callable, Iterator, Mapping
from datetime import timedelta

import httpx
import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth.models import User
from django.core.mail import EmailMessage
from django.test import override_settings

from pyracast.accounts.models import Membership
from pyracast.accounts.operations import reassign_workspace_admin
from pyracast.mail import smtp
from pyracast.mail.errors import NotSentError
from pyracast.mail.gmail import SEND_SCOPE, TOKEN_URL
from pyracast.notifications.jobs import process_notification
from pyracast.notifications.models import (
    DeliveryRecipient,
    NotificationDelivery,
    NotificationDeliveryStatus,
    RecipientRole,
    ReminderRecipient,
    ReminderSchedule,
)
from pyracast.notifications.operations import configure_reminders
from pyracast.notifications.recipients import placements, resolve
from pyracast.notifications.scheduler import enqueue_due
from tests.smtp_server import Script
from tests.support.concurrency import committed_elsewhere
from tests.support.domain import NOW, System, make_system
from tests.support.notification_groups import group, member
from tests.support.notification_transmission import OpeningBackend, replying
from tests.support.notifications import (
    SENDER,
    SLOT,
    StatementHook,
    after_statement,
    chosen,
    parsed,
    pending_address_change,
    smtp_wire,
)

pytestmark = pytest.mark.django_db(transaction=True)

TO, CC, BCC = RecipientRole.TO, RecipientRole.CC, RecipientRole.BCC
ADMIN = "admin@example.com"
EVERYONE = [
    "boss@example.com",
    "outside@example.com",
    ADMIN,
    "alice@example.com",
    "bob@example.com",
]


def workspace_with_members(*addresses: str) -> tuple[System, list[User]]:
    system = make_system(approved=True)
    system.member.email = ADMIN
    system.member.save(update_fields=["email"])
    EmailAddress.objects.create(user=system.member, email=ADMIN, verified=True, primary=True)
    return system, [member(system, address) for address in addresses]


def typed_schedule(system: System, addresses: list[str]) -> ReminderSchedule:
    return configure_reminders(
        system.member,
        system.tenant.pk,
        system.region.pk,
        enabled=True,
        recipients=addresses,
        times=["10:00"],
        language="en",
        now=NOW,
    )


def visible(
    schedule: ReminderSchedule, typed: Mapping[str, RecipientRole] | None = None
) -> ReminderSchedule:
    ReminderSchedule.objects.filter(pk=schedule.pk).update(hide_recipients=False)
    for key, role in (typed or {}).items():
        ReminderRecipient.objects.filter(schedule=schedule, key=key).update(role=role)
    schedule.refresh_from_db()
    return schedule


def visible_group() -> tuple[System, ReminderSchedule]:
    system, _ = workspace_with_members("alice@example.com", "bob@example.com")
    schedule = group(system, ["boss@example.com", "outside@example.com"])
    return system, visible(schedule, {"boss@example.com": TO})


def user(address: str) -> User:
    return User.objects.get(username=address)


def bound(schedule: ReminderSchedule, address: str, role: RecipientRole, *members: User) -> None:
    ReminderRecipient.objects.filter(schedule=schedule, key=address).update(
        role=role,
        role_members=sorted(
            Membership.objects.get(tenant_id=schedule.region.tenant_id, user=found).pk
            for found in members
        ),
    )


def moved(account: User, address: str) -> None:
    account.email = address
    account.save(update_fields=["email"])
    EmailAddress.objects.filter(user=account).update(email=address)


def delivered_roles(delivery_id: int) -> list[tuple[str, str]]:
    return list(
        DeliveryRecipient.objects.filter(delivery_id=delivery_id)
        .order_by("pk")
        .values_list("address", "role")
    )


def headers(message: EmailMessage) -> tuple[list[str], list[str], list[str]]:
    return list(message.to), list(message.cc), list(message.bcc)


def send_on(day: int = 0) -> int:
    slot = SLOT + timedelta(days=day)
    delivery_id = enqueue_due(slot)[-1]
    assert process_notification(delivery_id, slot)
    return delivery_id


def sent_on(day: int = 0, *, at_marker: Callable[[], object] | None = None) -> EmailMessage:
    try:
        with replying(lambda message: None) as sent:
            OpeningBackend.action = at_marker
            send_on(day)
            (message,) = sent
            return message
    finally:
        OpeningBackend.action = None


@pytest.fixture(name="smtp_wire")
def smtp_transactions() -> Iterator[Script]:
    with smtp_wire() as script:
        yield script


@pytest.fixture
def gmail_requests(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[bytes]]:
    sent: list[bytes] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if str(request.url) == TOKEN_URL:
            return httpx.Response(
                200, json={"access_token": "access", "token_type": "Bearer", "scope": SEND_SCOPE}
            )
        sent.append(base64.urlsafe_b64decode(json.loads(request.content)["raw"]))
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


def test_a_visible_schedule_sends_to_and_cc_headers_and_keeps_bcc_off_the_smtp_content(
    smtp_wire: Script,
) -> None:
    _, schedule = visible_group()
    chosen(schedule, user("bob@example.com"), BCC)

    delivery_id = send_on()

    (transaction,) = smtp_wire.transactions
    assert sorted(transaction.recipients) == sorted(EVERYONE)
    message = parsed(transaction.data)
    assert message["To"] == f"boss@example.com, {ADMIN}"
    assert message["Cc"] == "alice@example.com"
    assert message["Bcc"] is None
    assert b"outside@example.com" not in transaction.data
    assert b"bob@example.com" not in transaction.data
    assert delivered_roles(delivery_id) == [
        ("boss@example.com", TO),
        ("outside@example.com", BCC),
        (ADMIN, TO),
        ("alice@example.com", CC),
        ("bob@example.com", BCC),
    ]
    prepared = NotificationDelivery.objects.get(pk=delivery_id).prepared_message
    assert prepared is not None
    assert "@example.com" not in json.dumps(prepared)


def test_the_gmail_request_carries_to_cc_and_bcc_headers(gmail_requests: list[bytes]) -> None:
    visible_group()

    send_on()

    (raw,) = gmail_requests
    message = parsed(raw)
    assert message["To"] == f"boss@example.com, {ADMIN}"
    assert message["Cc"] == "alice@example.com, bob@example.com"
    assert message["Bcc"] == "outside@example.com"


def test_a_mail_with_copy_addresses_and_nobody_in_to_is_sent(smtp_wire: Script) -> None:
    system, _ = workspace_with_members()
    schedule = typed_schedule(system, ["first@example.com", "second@example.com"])
    visible(schedule, {"first@example.com": CC, "second@example.com": CC})

    send_on()

    (transaction,) = smtp_wire.transactions
    message = parsed(transaction.data)
    assert message["To"] is None
    assert message["Cc"] == "first@example.com, second@example.com"
    assert sorted(transaction.recipients) == ["first@example.com", "second@example.com"]


def test_a_recipient_narrowed_away_at_the_marker_is_in_no_header_and_defaults_follow() -> None:
    _, schedule = visible_group()

    message = sent_on(
        at_marker=lambda: ReminderRecipient.objects.filter(
            schedule=schedule, key="boss@example.com"
        ).delete()
    )

    assert headers(message) == (
        [ADMIN],
        ["alice@example.com", "bob@example.com"],
        ["outside@example.com"],
    )
    assert DeliveryRecipient.objects.filter(key="boss@example.com").count() == 0


def test_without_the_administrator_in_the_list_default_members_are_in_to() -> None:
    _, schedule = visible_group()
    ReminderRecipient.objects.filter(schedule=schedule, key="boss@example.com").delete()
    EmailAddress.objects.filter(email=ADMIN).update(verified=False)

    assert headers(sent_on()) == (
        ["alice@example.com", "bob@example.com"],
        [],
        ["outside@example.com"],
    )


def test_a_schedule_made_hidden_between_claim_and_marker_is_sent_hidden() -> None:
    _, schedule = visible_group()

    message = sent_on(
        at_marker=lambda: ReminderSchedule.objects.filter(pk=schedule.pk).update(
            hide_recipients=True
        )
    )

    to, cc, bcc = headers(message)
    assert (to, cc, sorted(bcc)) == ([], [], sorted(EVERYONE))
    assert message.message().get("To") is None
    assert set(DeliveryRecipient.objects.values_list("role", flat=True)) == {BCC}
    assert ReminderRecipient.objects.get(schedule=schedule, key="boss@example.com").role == TO


def test_after_a_rejected_attempt_the_retry_records_its_own_roles() -> None:
    _, schedule = visible_group()
    (delivery_id,) = enqueue_due(SLOT)
    attempts: list[EmailMessage] = []

    def reject_first(message: EmailMessage) -> None:
        attempts.append(message)
        if len(attempts) == 1:
            raise NotSentError("rejected")

    with replying(reject_first):
        assert not process_notification(delivery_id, SLOT)
        first = delivered_roles(delivery_id)
        delivery = NotificationDelivery.objects.get(pk=delivery_id)
        assert delivery.status == NotificationDeliveryStatus.RETRY
        ReminderRecipient.objects.filter(schedule=schedule, key="boss@example.com").update(role=BCC)
        assert process_notification(delivery_id, delivery.available_at)

    assert ("boss@example.com", TO) in first
    assert ("boss@example.com", BCC) in delivered_roles(delivery_id)
    assert headers(attempts[1])[0] == [ADMIN]


def test_a_members_hidden_choice_holds_across_an_address_change_and_lost_verification() -> None:
    _, schedule = visible_group()
    alice = user("alice@example.com")
    chosen(schedule, alice, BCC)
    alice.email = "alice.new@example.com"
    alice.save(update_fields=["email"])
    EmailAddress.objects.filter(user=alice).update(email=alice.email)

    assert "alice.new@example.com" in headers(sent_on(0))[2]

    EmailAddress.objects.filter(user=alice).update(verified=False)
    assert "alice.new@example.com" not in sent_on(1).recipients()

    EmailAddress.objects.filter(user=alice).update(verified=True)
    assert "alice.new@example.com" in headers(sent_on(2))[2]


def test_a_members_hidden_choice_wins_over_a_typed_to_of_the_same_address() -> None:
    system, (alice,) = workspace_with_members("alice@example.com")
    schedule = visible(
        configure_reminders(
            system.member,
            system.tenant.pk,
            system.region.pk,
            enabled=True,
            recipients=["alice@example.com"],
            include_workspace_members=True,
            times=["10:00"],
            language="en",
            now=NOW,
        ),
        {"alice@example.com": TO},
    )
    chosen(schedule, alice, BCC)

    assert headers(sent_on()) == ([ADMIN], [], ["alice@example.com"])


def test_a_typed_to_shows_a_hidden_member_only_while_the_members_address_is_unverified() -> None:
    system, (alice,) = workspace_with_members("alice@example.com")
    schedule = visible(
        configure_reminders(
            system.member,
            system.tenant.pk,
            system.region.pk,
            enabled=True,
            recipients=["alice@example.com"],
            include_workspace_members=True,
            times=["10:00"],
            language="en",
            now=NOW,
        ),
        {"alice@example.com": TO},
    )
    chosen(schedule, alice, BCC)

    def placed() -> list[tuple[str, RecipientRole]]:
        resolved = resolve(schedule)
        return list(
            zip(resolved.addresses, placements(schedule, resolved, resolved.addresses), strict=True)
        )

    assert sorted(placed()) == [(ADMIN, TO), ("alice@example.com", BCC)]
    assert headers(sent_on(0)) == ([ADMIN], [], ["alice@example.com"])

    EmailAddress.objects.filter(user=alice).update(verified=False)
    assert sorted(placed()) == [(ADMIN, TO), ("alice@example.com", TO)]
    message = sent_on(1)
    assert sorted(message.to) == [ADMIN, "alice@example.com"]
    assert (message.cc, message.bcc) == ([], [])

    EmailAddress.objects.filter(user=alice).update(verified=True)
    assert sorted(placed()) == [(ADMIN, TO), ("alice@example.com", BCC)]
    assert headers(sent_on(2)) == ([ADMIN], [], ["alice@example.com"])
    assert ReminderRecipient.objects.get(schedule=schedule).role == TO


def test_a_member_who_left_and_joined_again_follows_the_default() -> None:
    system, schedule = visible_group()
    alice = user("alice@example.com")
    chosen(schedule, alice, BCC)
    Membership.objects.filter(user=alice).delete()
    Membership.objects.create(tenant=system.tenant, user=alice)

    assert "alice@example.com" in headers(sent_on())[1]


def test_a_typed_to_whose_tied_member_gave_the_address_up_is_sent_hidden() -> None:
    system, (alice,) = workspace_with_members("alice@example.com")
    schedule = visible(typed_schedule(system, ["alice@example.com"]), {"alice@example.com": TO})
    alice.email = "alice.new@example.com"
    alice.save(update_fields=["email"])
    EmailAddress.objects.filter(user=alice).update(email=alice.email)

    assert headers(sent_on()) == ([], [], ["alice@example.com"])
    assert ReminderRecipient.objects.get(schedule=schedule).role == TO


def test_a_reassigned_administrator_and_a_joined_member_change_the_next_send() -> None:
    system, (alice,) = workspace_with_members("alice@example.com")
    schedule = visible(group(system))
    assert headers(sent_on(0)) == ([ADMIN], ["alice@example.com"], [])

    reassign_workspace_admin(
        system.admin, system.tenant.pk, alice.pk, expected_admin_id=system.member.pk
    )
    member(system, "carol@example.com")

    assert headers(sent_on(1)) == (["alice@example.com"], [ADMIN, "carol@example.com"], [])
    assert ReminderSchedule.objects.get(pk=schedule.pk).revision == schedule.revision


@pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])
def test_an_address_confirmed_after_the_markers_read_of_people_leaves_that_attempt_hidden(
    smtp_wire: Script, monkeypatch: pytest.MonkeyPatch
) -> None:
    system, (alice,) = workspace_with_members("alice@example.com")
    schedule = visible(group(system, ["alice@example.com"]))
    bound(schedule, "alice@example.com", TO, alice)
    chosen(schedule, alice, BCC)
    moved(alice, "alice.b@example.com")
    confirm = pending_address_change(alice, "alice@example.com")
    hook = StatementHook("accounts_membership", lambda: committed_elsewhere(confirm), armed=False)
    opening = smtp.EmailBackend.open

    def arm(backend: smtp.EmailBackend) -> bool | None:
        hook.armed = True
        return opening(backend)

    monkeypatch.setattr(smtp.EmailBackend, "open", arm)

    with after_statement(hook):
        delivery_id = send_on()

    assert hook.fired
    assert User.objects.get(pk=alice.pk).email == "alice@example.com"
    (transaction,) = smtp_wire.transactions
    assert "alice@example.com" in transaction.recipients
    assert parsed(transaction.data)["To"] == ADMIN
    assert b"alice@example.com" not in transaction.data
    assert ("alice@example.com", BCC) in delivered_roles(delivery_id)
