import re
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import Any

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth.models import User
from django.core import mail
from django.db import IntegrityError, connections
from django.test import Client, override_settings

from pyracast.accounts.models import Membership, Tenant
from pyracast.accounts.signup import CompanySignupInput
from tests.test_auth_lifecycle import submit

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])
PASSWORD = "test-only-" + "falcon-cloud-79!"
DETAILS = {"company_name": "Sunfield Energy", "email": "Owner@Example.COM", "password": PASSWORD}
AUTH = "/api/auth/browser/v1/"


@pytest.fixture(autouse=True)
def local_mail() -> Iterator[None]:
    with override_settings(
        MAILERS={"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}}
    ):
        yield


def mailed_code() -> str:
    for message in reversed(mail.outbox):
        for line in str(message.body).splitlines():
            if re.fullmatch(r"[A-Z0-9]{4}-[A-Z0-9]{4}", line.strip()):
                return line.strip()
    raise AssertionError("No authentication code was delivered.")


def signup(client: Client, overrides: dict[str, Any] | None = None) -> Any:
    token = client.get("/api/session/").json()["csrf_token"]
    return client.post(
        AUTH + "auth/signup",
        DETAILS | (overrides or {}),
        content_type="application/json",
        HTTP_X_CSRFTOKEN=token,
    )


def test_signup_requires_verification_before_tenant_access() -> None:
    other = Tenant.objects.create(name=DETAILS["company_name"])
    client = Client(enforce_csrf_checks=True)
    assert (
        client.post(AUTH + "auth/signup", DETAILS, content_type="application/json").status_code
        == 403
    )
    response = signup(client)
    assert response.status_code == 401
    assert not response.json()["meta"]["is_authenticated"]
    assert any(flow["id"] == "verify_email" for flow in response.json()["data"]["flows"])
    user = User.objects.get(email="owner@example.com")
    assert user.check_password(PASSWORD)
    assert not user.is_staff
    assert not user.is_superuser
    membership = Membership.objects.get(user=user)
    assert membership.tenant.name == DETAILS["company_name"]
    assert membership.tenant_id != other.pk
    assert not EmailAddress.objects.get(user=user).verified
    assert client.get("/api/session/").json()["user"] is None
    assert client.get("/api/tenants/").status_code in {401, 403}
    assert len(mail.outbox) == 1

    code = mailed_code()
    outsider = Client(enforce_csrf_checks=True)
    outsider_token = outsider.get("/api/session/").json()["csrf_token"]
    assert (
        outsider.post(
            AUTH + "auth/email/verify",
            {"key": code},
            content_type="application/json",
            HTTP_X_CSRFTOKEN=outsider_token,
        ).status_code
        == 409
    )
    assert not EmailAddress.objects.get(user=user).verified
    token = client.get("/api/session/").json()["csrf_token"]
    confirmed = client.post(
        AUTH + "auth/email/verify",
        {"key": code},
        content_type="application/json",
        HTTP_X_CSRFTOKEN=token,
    )
    assert confirmed.status_code == 200
    assert EmailAddress.objects.get(user=user).verified
    assert confirmed.json()["meta"]["is_authenticated"]
    token = client.get("/api/session/").json()["csrf_token"]
    assert client.delete(AUTH + "auth/session", HTTP_X_CSRFTOKEN=token).status_code == 401
    token = client.get("/api/session/").json()["csrf_token"]
    login = client.post(
        AUTH + "auth/login",
        {"email": "OWNER@example.com", "password": PASSWORD},
        content_type="application/json",
        HTTP_X_CSRFTOKEN=token,
    )
    assert login.status_code == 200
    assert login.json()["meta"]["is_authenticated"]
    assert client.get("/api/tenants/").json()["results"] == [
        {"id": membership.tenant_id, "name": DETAILS["company_name"]}
    ]
    assert client.get(f"/api/tenants/{other.pk}/regions/").status_code == 403


@pytest.mark.parametrize(
    "overrides",
    [
        {"company_name": " "},
        {"company_name": "x" * 201},
        {"email": "invalid"},
        {"password": "aB!57mQ8nP4z"},
    ],
)
def test_registration_rejects_invalid_input_without_partial_records(
    overrides: dict[str, Any],
) -> None:
    response = signup(Client(enforce_csrf_checks=True), overrides)
    assert response.status_code == 400
    assert not User.objects.exists()
    assert not Tenant.objects.exists()
    assert not Membership.objects.exists()
    assert not EmailAddress.objects.exists()


@override_settings(ACCOUNT_RATE_LIMITS={"confirm_email": "100/m/key"})
def test_duplicate_signup_does_not_disclose_or_join_existing_account() -> None:
    client = Client(enforce_csrf_checks=True)
    first = signup(client)
    user = User.objects.get()
    response = signup(Client(enforce_csrf_checks=True), {"company_name": "Attacker company"})
    assert response.status_code == first.status_code == 401
    assert response.json()["data"]["flows"] == first.json()["data"]["flows"]
    assert User.objects.count() == Tenant.objects.count() == Membership.objects.count() == 1
    assert Membership.objects.get().user == user
    assert user.check_password(PASSWORD)


def test_signup_rolls_back_user_tenant_and_email_if_membership_write_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(**kwargs: Any) -> None:
        raise IntegrityError("membership write failed")

    monkeypatch.setattr(Membership.objects, "create", fail)
    with pytest.raises(IntegrityError, match="membership write failed"):
        signup(Client(enforce_csrf_checks=True))
    assert not User.objects.exists()
    assert not Tenant.objects.exists()
    assert not EmailAddress.objects.exists()


def test_signup_preserves_password_whitespace_and_bounds_hash_input() -> None:
    client = Client(enforce_csrf_checks=True)
    password = "  " + PASSWORD + "  "
    assert signup(client, {"password": password}).status_code == 401
    user = User.objects.get()
    assert user.check_password(password)
    assert not user.check_password(password.strip())
    assert (
        signup(
            Client(enforce_csrf_checks=True),
            {"email": "another@example.com", "password": "x" * 1025},
        ).status_code
        == 400
    )
    assert User.objects.count() == 1


def test_signup_code_attempt_limit_rejects_the_correct_code_after_abort() -> None:
    client = Client(enforce_csrf_checks=True)
    assert signup(client).status_code == 401
    code = mailed_code()
    token = client.get("/api/session/").json()["csrf_token"]
    for _ in range(3):
        assert (
            client.post(
                AUTH + "auth/email/verify",
                {"key": "WRONG-CODE"},
                content_type="application/json",
                HTTP_X_CSRFTOKEN=token,
            ).status_code
            == 400
        )
    assert (
        client.post(
            AUTH + "auth/email/verify",
            {"key": code},
            content_type="application/json",
            HTTP_X_CSRFTOKEN=token,
        ).status_code
        == 409
    )
    assert not EmailAddress.objects.get().verified


def test_concurrent_signup_for_one_mailbox_can_be_recovered_by_its_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validation = Barrier(2)
    address_save = Barrier(2)
    User.objects.create_user(username="owner")
    clean_email = CompanySignupInput.clean_email
    save = EmailAddress.save

    def synchronized_email(form: CompanySignupInput) -> str:
        email: str = clean_email(form)
        validation.wait(timeout=10)
        return email

    def synchronized_address_save(address: EmailAddress, *args: Any, **kwargs: Any) -> None:
        if address._state.adding and not address.verified:
            address_save.wait(timeout=10)
        save(address, *args, **kwargs)

    monkeypatch.setattr(CompanySignupInput, "clean_email", synchronized_email)
    monkeypatch.setattr(EmailAddress, "save", synchronized_address_save)

    def register(order: str) -> int:
        client = Client(enforce_csrf_checks=True)
        try:
            token = client.get("/api/session/").json()["csrf_token"]
            return client.post(
                AUTH + "auth/signup",
                DETAILS,
                content_type="application/json",
                HTTP_X_CSRFTOKEN=token,
                HTTP_X_REGISTRATION_ORDER=order,
            ).status_code
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(register, ("first", "second")))
    assert all(status in {401, 429} for status in statuses)
    assert User.objects.count() == 3
    assert EmailAddress.objects.filter(primary=True, verified=False).count() == 2
    assert Tenant.objects.count() == Membership.objects.count() == 2
    assert not EmailAddress.objects.filter(verified=True).exists()
    owner = Client(enforce_csrf_checks=True)
    assert (
        submit(owner, "auth/login", {"email": DETAILS["email"], "password": PASSWORD}).status_code
        == 400
    )
    mail.outbox.clear()
    assert submit(owner, "auth/password/request", {"email": DETAILS["email"]}).status_code == 401
    code = mailed_code()
    assert (
        submit(
            owner, "auth/password/reset", {"key": code, "password": PASSWORD + "owned"}
        ).status_code
        == 401
    )
    identity = EmailAddress.objects.get(verified=True)
    assert identity.user_id == min(EmailAddress.objects.values_list("user_id", flat=True))
    assert (
        submit(
            owner, "auth/login", {"email": DETAILS["email"], "password": PASSWORD + "owned"}
        ).status_code
        == 200
    )
