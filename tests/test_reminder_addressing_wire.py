import base64
import json
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import httpx
import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth.models import User
from django.core import mail
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.test import Client, override_settings
from django.utils import timezone

from pyracast.accounts.models import Membership
from pyracast.mail import smtp
from pyracast.mail.gmail import SEND_SCOPE, TOKEN_URL
from pyracast.notifications.jobs import process_notification
from pyracast.notifications.models import (
    DeliveryRecipient,
    NotificationDelivery,
    NotificationDeliveryStatus,
    NotificationSettings,
    RecipientRole,
    ReminderRecipient,
    ReminderSchedule,
)
from pyracast.notifications.operations import configure_reminders
from pyracast.notifications.recipients import mailbox_key, prepared, recipient_key
from pyracast.notifications.scheduler import enqueue_due
from tests.support.domain import NOW, System, make_system
from tests.support.notification_groups import member
from tests.support.notifications import (
    SENDER,
    SLOT,
    parsed,
    pending_address_change,
    smtp_wire,
    store,
)

TO, CC, BCC = RecipientRole.TO, RecipientRole.CC, RecipientRole.BCC
ADMIN = "admin@example.com"
SOFT_HYPHEN, QUOTED = "ops@soft\u00admail.de", '"ops"@example.com'
UNICODE, ACE = "ops@b\u00fccher.de", "ops@xn--bcher-kva.de"
DISTINCT = ("ops@xn--strae-oqa8r.de", "ops@xn--strasse-t2a.de")
REFUSED = "ops@a\ufffdb.de"

ONE_KEY = [
    (SOFT_HYPHEN, "ops@softmail.de"),
    (QUOTED, "ops@example.com"),
    (UNICODE, ACE),
    ("ops@B\u00dcCHER.DE", ACE),
    ("OPS@XN--BCHER-KVA.DE", ACE),
    ("ops@\uff42\u00fccher.de", UNICODE),
    ("ops@\uff46\uff55\uff4c\uff4c.de", "ops@full.de"),
]
STORED_APART = [pair for pair in ONE_KEY if recipient_key(pair[0]) != recipient_key(pair[1])]
SPELLINGS = list(dict.fromkeys([*(spelling for pair in ONE_KEY for spelling in pair), *DISTINCT]))


def test_every_pair_of_one_key_is_one_prepared_address_and_distinct_ace_domains_stay_two() -> None:
    for spelling, other in ONE_KEY:
        assert mailbox_key(spelling) == mailbox_key(other) == recipient_key(prepared(other))
    assert mailbox_key(DISTINCT[0]) != mailbox_key(DISTINCT[1])
    assert [prepared(address) for address in DISTINCT] == list(DISTINCT)


def test_a_spelling_the_email_validator_refuses_is_refused_before_it_is_keyed() -> None:
    with pytest.raises(ValidationError):
        validate_email("ops@b\u00fccher\u3002de")


@pytest.mark.parametrize("address", SPELLINGS)
def test_the_prepared_form_is_the_configured_smtp_backends_and_its_own(address: str) -> None:
    with smtp_wire():
        configured = mail.mailers["default"]
        assert isinstance(configured, smtp.EmailBackend)
        assert prepared(address) == configured.prep_address(address)
    assert prepared(prepared(address)) == prepared(address)
    assert mailbox_key(prepared(address)) == mailbox_key(address)


def test_an_address_the_library_refuses_raises_and_has_a_key_no_prepared_form_has() -> None:
    validate_email(REFUSED)
    with pytest.raises(UnicodeError):
        prepared(REFUSED)
    assert mailbox_key(REFUSED) not in {recipient_key(prepared(address)) for address in SPELLINGS}
    assert mailbox_key(REFUSED) == mailbox_key(REFUSED.upper())


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


def request(system: System, suffix: str = "", **changes: Any) -> Any:
    client = Client()
    client.force_login(system.member)
    body = {
        "enabled": True,
        "include_workspace_members": True,
        "language": "en",
        "recipients": [],
        "times": ["10:00"],
        "expected_revision": revision(system),
        "hide_recipients": False,
    } | changes
    send = client.post if suffix else client.put
    return send(url(system, suffix), body, content_type="application/json")


def send_on(day: int = 0) -> int:
    slot = SLOT + timedelta(days=day)
    delivery_id = enqueue_due(slot)[-1]
    process_notification(delivery_id, slot)
    return delivery_id


@pytestdb
@pytest.mark.parametrize(("spelling", "other"), ONE_KEY)
def test_two_spellings_of_one_key_are_refused_as_duplicates(spelling: str, other: str) -> None:
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
def test_an_address_the_mail_library_refuses_is_refused_at_the_save() -> None:
    system = workspace()

    response = request(system, recipients=[REFUSED])

    assert response.status_code == 400, response.content
    assert list(response.json()["fields"]) == ["recipients"]
    assert not ReminderSchedule.objects.exists()


@pytestdb
@pytest.mark.parametrize(("spelling", "other"), STORED_APART)
def test_stored_spellings_of_one_key_combine_and_are_sent_once_in_their_prepared_form(
    spelling: str, other: str
) -> None:
    system = workspace()
    schedule = configure_reminders(
        system.member,
        system.tenant.pk,
        system.region.pk,
        enabled=True,
        recipients=["first@example.com"],
        include_workspace_members=False,
        times=["10:00"],
        language="en",
        now=NOW,
        hide_recipients=False,
    )
    store(schedule, [spelling, other])
    ReminderRecipient.objects.filter(schedule=schedule, key=recipient_key(spelling)).update(role=TO)

    with smtp_wire() as script:
        send_on()

    (transaction,) = script.transactions
    message = parsed(transaction.data)
    assert (message["To"], message["Cc"]) == (None, None)
    assert transaction.recipients == [prepared(spelling)]


def typed_roles() -> list[tuple[str, RecipientRole]]:
    return [
        (SOFT_HYPHEN, TO),
        (QUOTED, CC),
        (UNICODE, BCC),
        ("ops@M\u00dcNCHEN.DE", CC),
        ("ops@\uff46\uff55\uff4c\uff4c.de", TO),
        (DISTINCT[0], TO),
        (DISTINCT[1], CC),
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


def prepared_of(role: RecipientRole) -> list[str]:
    return [prepared(address) for address, given in typed_roles() if given == role]


@pytestdb
@pytest.mark.parametrize("eight_bit", [True, False])
def test_the_smtp_headers_and_envelope_carry_each_prepared_form(eight_bit: bool) -> None:
    system = workspace()
    typed_schedule(system)

    with smtp_wire() as script:
        if not eight_bit:
            script.extensions = ["SIZE 10000000"]
        send_on()

    (transaction,) = script.transactions
    assert transaction.mail_from.upper().endswith(" BODY=8BITMIME") == eight_bit
    message = parsed(transaction.data)
    assert message["To"] == ", ".join(prepared_of(TO))
    assert message["Cc"] == ", ".join(prepared_of(CC))
    assert message["Bcc"] is None
    assert transaction.recipients == prepared_of(TO) + prepared_of(CC) + prepared_of(BCC)
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
def test_the_gmail_request_carries_each_prepared_form_and_a_unicode_bcc_domain_sends(
    gmail_requests: list[bytes],
) -> None:
    system = workspace()
    typed_schedule(system)

    delivery_id = send_on()

    assert NotificationDelivery.objects.get(pk=delivery_id).status == "sent"
    (raw,) = gmail_requests
    message = parsed(raw)
    assert message["To"] == ", ".join(prepared_of(TO))
    assert message["Cc"] == ", ".join(prepared_of(CC))
    assert message["Bcc"] == ", ".join(prepared_of(BCC)) == ACE


@pytestdb
@pytest.mark.parametrize(
    ("typed", "held"), [(SOFT_HYPHEN, "ops@softmail.de"), (QUOTED, "ops@example.com")]
)
def test_a_typed_to_of_a_hidden_members_address_in_another_spelling_is_in_no_header(
    typed: str, held: str
) -> None:
    system = workspace()
    holder = member(system, held)
    preview = request(system, "preview/", recipients=[typed]).json()
    (answered,) = preview["addresses"]["added"]
    response = request(
        system,
        recipients=[typed],
        address_roles=[{"address": typed, "role": "to", "memberships": answered["memberships"]}],
        member_roles=[{"memberships": [membership(holder)], "address": held, "role": "bcc"}],
    )
    assert response.status_code == 200, response.content

    with smtp_wire() as script:
        send_on()

    (transaction,) = script.transactions
    message = parsed(transaction.data)
    assert (message["To"], message["Cc"]) == (ADMIN, None)
    assert b"ops@" not in transaction.data
    assert transaction.recipients.count(held) == 1
    assert sorted(transaction.recipients) == sorted([held, ADMIN])


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
    assert script.transactions[0].recipients == [second]


@pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])
def test_an_account_cannot_confirm_an_address_the_mail_library_refuses() -> None:
    system = workspace()
    holder = member(system, "holder@example.com")

    with pytest.raises(AssertionError, match="No authentication code"):
        pending_address_change(holder, REFUSED)

    assert User.objects.get(pk=holder.pk).email == "holder@example.com"
    assert not EmailAddress.objects.filter(email=REFUSED, verified=True).exists()


@pytestdb
def test_a_member_address_the_library_refuses_is_sent_as_stored_and_the_message_refused() -> None:
    system = workspace()
    member(system, REFUSED)
    assert request(system).status_code == 200

    with smtp_wire() as script:
        delivery_id = send_on()

    assert script.transactions == []
    delivery = NotificationDelivery.objects.get(pk=delivery_id)
    assert delivery.status == NotificationDeliveryStatus.FAILED
    assert set(DeliveryRecipient.objects.values_list("address", flat=True)) == {ADMIN, REFUSED}
