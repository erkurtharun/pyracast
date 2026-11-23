from io import StringIO
from typing import Any

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth.models import User
from django.core import mail
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.core.validators import validate_email
from django.test import Client, override_settings

from pyracast.accounts.addresses import plain_address
from pyracast.accounts.models import OutboundMail, Tenant, WorkspaceInvitation
from pyracast.mail import smtp
from pyracast.notifications.recipients import recipient_key
from tests.support.admin_mfa_bootstrap import Terminal
from tests.support.api import verified_admin
from tests.support.auth_lifecycle import PASSWORD, submit
from tests.support.company_registration import signup
from tests.support.notifications import smtp_wire
from tests.support.social_accounts import PROVIDERS, begin, complete, serve_provider_profile
from tests.support.workspaces import administered_workspace, post, signed_in

LOCAL_MAIL = {"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}}
SPECIALS = "!#$%&'*+/=?^_`{|}~-"
LONGEST = "a" * 64 + "@" + ".".join(["b" * 63, "c" * 63, "d" * 57, "com"])

ACCEPTED = {
    "ordinary": "ops@example.com",
    "64-octet local part": "a" * 64 + "@example.com",
    "63-octet label": "ops@" + "b" * 63 + ".com",
    "254 octets": LONGEST,
    "every dot-atom special": f"a{SPECIALS}z@example.com",
    "dotted local part": "first.last.name@example.com",
    "digits and hyphens inside labels": "ops@mail-1.example-2.com",
    "all-digit inner label": "ops@123.example.com",
    "ace label": "ops@xn--bcher-kva.de",
    "mixed case": "Ops.Team@Example.COM",
}
REFUSED = {
    "non-ascii local part": "t\u017fst@example.com",
    "kelvin sign": "t\u212ast@example.com",
    "unicode domain": "ops@b\u00fccher.de",
    "full-width letters": "ops@\uff46\uff55\uff4c\uff4c.de",
    "soft hyphen": "ops@soft\xadmail.de",
    "255 octets": LONGEST.replace("d" * 57, "d" * 58),
    "two at signs": "a@b@example.com",
    "quoted at sign": '"a@b"@example.com',
    "empty local part": "@example.com",
    "65-octet local part": "a" * 65 + "@example.com",
    "leading dot": ".ops@example.com",
    "trailing dot": "ops.@example.com",
    "double dot": "o..ps@example.com",
    "quoted local part": '"ops"@example.com',
    "quoted local part with an escape": '"a\\ b"@example.com',
    "quoted local part with a control": '"a\\\tb"@example.com',
    "comment": "ops(team)@example.com",
    "trailing comment": "ops@example.com (team)",
    "display name": "Ops <ops@example.com>",
    "ipv4 literal": "ops@[192.0.2.1]",
    "full-width ipv4 literal": "ops@\uff3b192.0.2.\uff10\uff11\uff3d",
    "tagged ipv6 literal": "ops@[IPv6:2001:db8::1]",
    "untagged ipv6 literal": "ops@[2001:db8::1]",
    "single-label domain": "ops@localhost",
    "64-octet label": "ops@" + "b" * 64 + ".com",
    "leading hyphen": "ops@-example.com",
    "trailing hyphen": "ops@example-.com",
    "underscore in a label": "x@ex_ample.com",
    "full-width underscore": "x@ex\uff3fample.com",
    "all-digit last label": "ops@example.123",
    "trailing dot in the domain": "ops@example.com.",
    "embedded space": "o ps@example.com",
    "surrounding space": " ops@example.com",
    "nul": "ops\x00@example.com",
    "carriage return": "ops@exa\rmple.com",
    "line feed": "ops@example.com\n",
}


@pytest.mark.parametrize("address", list(ACCEPTED.values()), ids=list(ACCEPTED))
def test_a_plain_address_is_kept_as_typed_keyed_by_its_lower_case_and_sent_unchanged(
    address: str,
) -> None:
    assert plain_address(address) == address
    assert recipient_key(address) == address.lower()
    with smtp_wire():
        configured = mail.mailers["default"]
        assert isinstance(configured, smtp.EmailBackend)
        assert configured.prep_address(address) == address


def test_the_longest_plain_address_has_254_octets() -> None:
    assert len(LONGEST) == 254
    assert len(REFUSED["255 octets"]) == 255


@pytest.mark.parametrize("address", list(REFUSED.values()), ids=list(REFUSED))
def test_an_address_that_is_not_plain_is_refused_with_the_invalid_address_error(
    address: str,
) -> None:
    with pytest.raises(ValidationError) as refused:
        plain_address(address)

    assert (refused.value.code, refused.value.message) == (
        validate_email.code,
        validate_email.message,
    )


# Each passes Django's address validator, so only the plain-form check refuses it.
ACCEPTED_BY_DJANGO = [
    "ops@b\u00fccher.de",
    '"ops"@example.com',
    "ops@[192.0.2.1]",
    "t\u017fst@example.com",
]
INVALID = "Enter a valid email address."
accounts = pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])


@pytest.mark.parametrize("address", ACCEPTED_BY_DJANGO)
def test_django_accepts_each_address_the_account_proofs_use(address: str) -> None:
    validate_email(address)


@accounts
@pytest.mark.parametrize("address", ACCEPTED_BY_DJANGO)
def test_sign_up_refuses_an_address_that_is_not_plain(address: str) -> None:
    with override_settings(MAILERS=LOCAL_MAIL):
        response = signup(Client(enforce_csrf_checks=True), {"email": address})

    assert response.status_code == 400, response.content
    assert response.json()["fields"] == {"email": INVALID}
    assert not User.objects.exists()
    assert not Tenant.objects.exists()


def signed_in_with_password() -> tuple[Client, User]:
    user = User.objects.create_user(
        username="holder", email="holder@example.com", password=PASSWORD
    )
    EmailAddress.objects.create(user=user, email=user.email, primary=True, verified=True)
    client = Client(enforce_csrf_checks=True)
    assert (
        submit(client, "auth/login", {"email": user.email, "password": PASSWORD}).status_code == 200
    )
    assert submit(client, "auth/reauthenticate", {"password": PASSWORD}).status_code == 200
    return client, user


@accounts
@pytest.mark.parametrize("address", ACCEPTED_BY_DJANGO)
def test_adding_an_address_that_is_not_plain_is_refused(address: str) -> None:
    client, user = signed_in_with_password()

    with override_settings(MAILERS=LOCAL_MAIL):
        response = submit(client, "account/email", {"email": address})

    assert response.status_code == 400, response.content
    assert response.json()["fields"] == {"email": INVALID}
    assert list(EmailAddress.objects.values_list("email", flat=True)) == [user.email]
    assert mail.outbox == []


@accounts
@pytest.mark.parametrize("address", ACCEPTED_BY_DJANGO)
def test_an_invitation_over_the_api_refuses_an_address_that_is_not_plain(address: str) -> None:
    tenant, administrator = administered_workspace()

    response = post(
        signed_in(administrator), f"/api/tenants/{tenant.pk}/invitations/", {"email": address}
    )

    assert response.status_code == 400, response.content
    assert response.json()["fields"] == {"email": INVALID}
    assert not WorkspaceInvitation.objects.exists()


@accounts
@pytest.mark.parametrize("address", ACCEPTED_BY_DJANGO)
def test_the_console_refuses_to_invite_an_address_that_is_not_plain(address: str) -> None:
    tenant = Tenant.objects.create(name="Solar workspace")
    client, _ = verified_admin()

    invitation = client.post(
        f"/admin/workspaces/{tenant.pk}/invitations/new/", {"email": address, "language": "en"}
    )
    creation = client.post(
        "/admin/workspaces/new/",
        {"name": "Plain only", "administrator_email": address, "language": "en"},
    )

    for response in (invitation, creation):
        assert response.status_code == 400
        assert INVALID in response.content.decode()
    assert not WorkspaceInvitation.objects.exists()
    assert not Tenant.objects.filter(name="Plain only").exists()


@pytest.fixture
def oauth_profile(monkeypatch: pytest.MonkeyPatch) -> Any:
    with override_settings(SOCIALACCOUNT_PROVIDERS=PROVIDERS, MAILERS=LOCAL_MAIL):
        yield serve_provider_profile(monkeypatch)


@accounts
@pytest.mark.parametrize("address", ACCEPTED_BY_DJANGO)
def test_the_provider_sign_up_refuses_a_typed_address_that_is_not_plain(
    oauth_profile: dict[str, Any], address: str
) -> None:
    client = Client(enforce_csrf_checks=True)
    complete(client, begin(client))

    response = submit(
        client, "auth/provider/signup", {"email": address, "company_name": "Social Solar"}
    )

    assert response.status_code == 400, response.content
    assert response.json()["fields"] == {"email": INVALID}
    assert not User.objects.exists()


@accounts
@pytest.mark.parametrize("provider", ["google", "microsoft"])
def test_an_address_a_provider_supplies_that_is_not_plain_is_never_offered_or_stored(
    oauth_profile: dict[str, Any], provider: str
) -> None:
    oauth_profile.update(email="ops@b\u00fccher.de", mail="ops@b\u00fccher.de")
    client = Client(enforce_csrf_checks=True)
    complete(client, begin(client, provider), provider)

    offered = client.get("/api/auth/browser/v1/auth/provider/signup").json()["data"]["email"]
    response = submit(
        client,
        "auth/provider/signup",
        {"email": "owner@example.com", "company_name": "Social Solar"},
    )

    assert offered == []
    assert response.status_code in {200, 401}, response.content
    user = User.objects.get()
    assert user.email == "owner@example.com"
    assert list(EmailAddress.objects.values_list("email", flat=True)) == ["owner@example.com"]


@accounts
@pytest.mark.parametrize("address", ACCEPTED_BY_DJANGO)
def test_the_administrator_bootstrap_refuses_an_address_that_is_not_plain(address: str) -> None:
    shown = StringIO()
    terminal = Terminal(shown, [])

    with pytest.raises(CommandError) as refused:
        call_command(
            "bootstrap_admin", email=address, stdin=terminal, stdout=shown, stderr=StringIO()
        )

    assert str(refused.value) == INVALID
    assert terminal.asked == 0
    assert not User.objects.exists()


@accounts
def test_a_password_reset_for_an_account_address_that_is_not_plain_is_still_mailed() -> None:
    held = "ops@b\u00fccher.de"
    user = User.objects.create_user(username="held", email=held, password=PASSWORD)
    EmailAddress.objects.create(user=user, email=held, primary=True, verified=True)

    with override_settings(MAILERS=LOCAL_MAIL):
        response = submit(
            Client(enforce_csrf_checks=True), "auth/password/request", {"email": held}
        )

    assert response.status_code in {200, 401}, response.content
    assert list(OutboundMail.objects.values_list("recipient", flat=True)) == [held]
