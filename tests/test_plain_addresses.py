import time
from io import StringIO
from types import SimpleNamespace
from typing import Any

import pytest
from allauth.account.models import EmailAddress
from allauth.mfa.models import Authenticator
from allauth.mfa.totp.internal import auth as totp_auth
from allauth.mfa.totp.internal.auth import TOTP, generate_totp_secret
from django.contrib.auth.models import User
from django.core import mail
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.core.validators import validate_email
from django.test import Client, override_settings

from pyracast.accounts.addresses import is_plain, plain_address
from pyracast.accounts.models import OutboundMail, Tenant, WorkspaceInvitation
from pyracast.mail import smtp
from pyracast.notifications.recipients import recipient_key
from tests.support.admin_mfa_bootstrap import (
    NEW_PASSWORD,
    Terminal,
    enrollment_secret,
    run,
)
from tests.support.api import verified_admin
from tests.support.auth_lifecycle import PASSWORD, submit, totp_code
from tests.support.company_registration import signup
from tests.support.notifications import smtp_wire
from tests.support.social_accounts import PROVIDERS, begin, complete, serve_provider_profile
from tests.support.workspaces import administered_workspace, post, signed_in

LOCAL_MAIL = {"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}}
REMOVED_SPECIALS = "!#$%&'*/=?^`{|}~"
LONGEST = "a" * 64 + "@" + ".".join(["b" * 63, "c" * 63, "d" * 57, "com"])

ACCEPTED = {
    "ordinary": "ops@example.com",
    "64-octet local part": "a" * 64 + "@example.com",
    "63-octet label": "ops@" + "b" * 63 + ".com",
    "254 octets": LONGEST,
    "dot inside a local part": "first.last.name@example.com",
    "underscore inside a local part": "first_last@example.com",
    "plus inside a local part": "first+tag@example.com",
    "hyphen inside a local part": "first-last@example.com",
    "each of the four together": "a.b_c+d-e@example.com",
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
    **{
        f"{special!r} inside a local part": f"a{special}b@example.com"
        for special in REMOVED_SPECIALS
    },
    "an encoded word opening across two addresses": "=?utf-8?q?alice@example.org",
    "an encoded word closing across two addresses": "bob?=@example.net",
    "an encoded word that lower-casing changes": "=?utf-16?b?YQ==?=@example.com",
    "kelvin sign in the domain": "ops@\u212aelvin.com",
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


@pytest.mark.parametrize("address", list(ACCEPTED.values()), ids=list(ACCEPTED))
def test_a_plain_address_stays_plain_in_lower_and_in_upper_case(address: str) -> None:
    assert (is_plain(address.lower()), is_plain(address.upper())) == (True, True)


@pytest.mark.parametrize(
    "address",
    [*ACCEPTED.values(), *REFUSED.values()],
    ids=[*(f"accepted {name}" for name in ACCEPTED), *(f"refused {name}" for name in REFUSED)],
)
def test_an_address_and_its_identity_are_both_plain_or_both_not(address: str) -> None:
    assert is_plain(recipient_key(address)) == is_plain(address)


def test_the_identity_lower_cases_only_ascii_letters_of_the_exact_text() -> None:
    assert [recipient_key(text) for text in ("Ops@\u212aelvin.COM", " A@B.de", "\u0130@X.de")] == [
        "ops@\u212aelvin.com",
        " a@b.de",
        "\u0130@x.de",
    ]


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
    "=?utf-16?b?YQ==?=@example.com",
]
SUBMITTED, STORED = " New.Member+Tag@Example.COM ", "new.member+tag@example.com"
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


@accounts
def test_an_invitation_stores_the_lower_cased_address() -> None:
    tenant, administrator = administered_workspace()
    client, _ = verified_admin()

    over_the_api = post(
        signed_in(administrator), f"/api/tenants/{tenant.pk}/invitations/", {"email": SUBMITTED}
    )
    WorkspaceInvitation.objects.all().delete()
    over_the_console = client.post(
        f"/admin/workspaces/{tenant.pk}/invitations/new/", {"email": SUBMITTED, "language": "en"}
    )

    assert over_the_api.status_code == 201, over_the_api.content
    assert over_the_console.status_code == 302
    stored = WorkspaceInvitation.objects.get().email
    assert (stored, is_plain(stored)) == (STORED, True)
    assert over_the_api.json()["email"] == STORED


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
    held = [user.email, *EmailAddress.objects.values_list("email", flat=True)]
    assert held == ["owner@example.com", "owner@example.com"]
    assert all(is_plain(address) for address in held)


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
def test_the_administrator_bootstrap_stores_the_lower_cased_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    moment = time.time()
    monkeypatch.setattr(totp_auth, "time", SimpleNamespace(time=lambda: moment))

    def code(shown: str) -> str:
        return totp_code(enrollment_secret(shown), moment)

    run("bootstrap_admin", [NEW_PASSWORD, NEW_PASSWORD, code], email=SUBMITTED)

    user = User.objects.get()
    held = [user.email, *EmailAddress.objects.values_list("email", flat=True)]
    assert held == [STORED, STORED]
    assert all(is_plain(address) for address in held)


@accounts
def test_a_second_factor_reset_finds_an_account_whose_address_is_not_plain() -> None:
    held = "ops@b\u00fccher.de"
    user = User.objects.create_user(username="held", email=held, is_staff=True)
    EmailAddress.objects.create(user=user, email=held, primary=True, verified=True)
    TOTP.activate(user, generate_totp_secret())

    shown, terminal = run("reset_mfa", [], email=held, reason="lost_device", dry_run=True)

    assert shown.startswith(f"Dry run: would remove 1 second factor and end 0 sessions of {held}.")
    assert terminal.asked == 0
    assert Authenticator.objects.filter(user=user).count() == 1


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
