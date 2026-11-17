import re
import time
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from allauth.account.models import EmailAddress
from allauth.core.internal import ratelimit
from django.contrib.auth.models import User
from django.core import mail
from django.test import Client, override_settings

from tests.support.auth_lifecycle import AUTH, PASSWORD, member, submit
from tests.support.company_registration import signup

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])
CODE = re.compile(r"^\s*([A-Z0-9]{4}-[A-Z0-9]{4}|[0-9]{6})\s*$", re.MULTILINE)
PENDING = {"id": "verify_email", "is_pending": True}


@pytest.fixture(autouse=True)
def local_mail() -> Iterator[None]:
    with override_settings(
        MAILERS={"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}}
    ):
        yield


def browser() -> Client:
    return Client(enforce_csrf_checks=True)


def mailed_code() -> str:
    (message,) = mail.outbox
    match = CODE.search(str(message.body))
    assert match is not None
    mail.outbox.clear()
    return match.group(1)


def one_minute_later(monkeypatch: pytest.MonkeyPatch) -> None:
    later = time.time() + 61
    monkeypatch.setattr(
        ratelimit,
        "time",
        SimpleNamespace(time=lambda: later, monotonic=time.monotonic, sleep=time.sleep),
    )


def answer(response: Any) -> tuple[int, Any]:
    return response.status_code, response.json()


def signed_in(user: User) -> Client:
    client = browser()
    credentials = {"email": user.email, "password": PASSWORD}
    assert submit(client, "auth/login", credentials).status_code == 200
    assert submit(client, "auth/reauthenticate", {"password": PASSWORD}).status_code == 200
    return client


def pending_signup() -> tuple[Client, str]:
    client = browser()
    assert signup(client).status_code == 401
    return client, mailed_code()


def reset_elsewhere(email: str) -> None:
    client = browser()
    assert submit(client, "auth/password/request", {"email": email}).status_code == 401
    reset = {"key": mailed_code(), "password": PASSWORD + "reset"}
    assert submit(client, "auth/password/reset", reset).status_code == 401
    mail.outbox.clear()


def assert_signed_out_without_pending_verification(client: Client) -> None:
    assert client.get("/api/session/").json()["user"] is None
    assert PENDING not in client.get(AUTH + "auth/session").json()["data"]["flows"]


def test_signup_code_for_an_address_verified_meanwhile_answers_like_no_pending_verification() -> (
    None
):
    client, code = pending_signup()
    reset_elsewhere("owner@example.com")
    expected = answer(submit(browser(), "auth/email/verify", {"key": code}))

    response = submit(client, "auth/email/verify", {"key": code})

    assert answer(response) == expected
    assert mail.outbox == []
    assert_signed_out_without_pending_verification(client)


def test_signup_resend_for_an_address_verified_meanwhile_mails_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = pending_signup()
    reset_elsewhere("owner@example.com")
    one_minute_later(monkeypatch)
    expected = answer(submit(browser(), "auth/email/verify/resend", {}))

    response = submit(client, "auth/email/verify/resend", {})

    assert answer(response) == expected
    assert mail.outbox == []
    assert_signed_out_without_pending_verification(client)


def change_pending(user: User) -> tuple[Client, str]:
    client = signed_in(user)
    assert submit(client, "account/email", {"email": "new@example.com"}).status_code == 200
    return client, mailed_code()


def test_change_code_for_an_address_verified_out_of_band_answers_like_no_pending_change() -> None:
    user = member()
    client, code = change_pending(user)
    EmailAddress.objects.create(user=user, email="new@example.com", verified=True)
    methods = client.get(AUTH + "auth/session").json()["data"]["methods"]
    expected = answer(submit(signed_in(user), "auth/email/verify", {"key": code}))

    response = submit(client, "auth/email/verify", {"key": code})

    assert answer(response) == expected
    assert mail.outbox == []
    assert client.get(AUTH + "auth/session").json()["data"]["methods"] == methods
    assert EmailAddress.objects.get(user=user, primary=True).email == "member@example.com"


def test_change_resend_for_an_address_verified_out_of_band_mails_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = member()
    client, _ = change_pending(user)
    EmailAddress.objects.create(user=user, email="new@example.com", verified=True)
    one_minute_later(monkeypatch)
    expected = answer(submit(signed_in(user), "auth/email/verify/resend", {}))

    response = submit(client, "auth/email/verify/resend", {})

    assert answer(response) == expected
    assert mail.outbox == []


@pytest.mark.parametrize("endpoint", ["auth/email/verify", "auth/email/verify/resend"])
def test_verification_for_a_deleted_signup_account_answers_like_no_pending_verification(
    endpoint: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, code = pending_signup()
    User.objects.filter(email="owner@example.com").delete()
    one_minute_later(monkeypatch)
    expected = answer(submit(browser(), endpoint, {"key": code}))

    response = submit(client, endpoint, {"key": code})

    assert answer(response) == expected
    assert mail.outbox == []
    assert_signed_out_without_pending_verification(client)


def test_reset_for_a_deleted_account_answers_like_no_pending_reset() -> None:
    user = member()
    client = browser()
    assert submit(client, "auth/password/request", {"email": user.email}).status_code == 401
    reset = {"key": mailed_code(), "password": PASSWORD + "reset"}
    User.objects.filter(pk=user.pk).delete()
    expected = answer(submit(browser(), "auth/password/reset", reset))

    response = submit(client, "auth/password/reset", reset)

    assert answer(response) == expected
    assert mail.outbox == []
    assert client.get("/api/session/").json()["user"] is None
