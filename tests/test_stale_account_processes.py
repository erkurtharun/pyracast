import re
import time
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from allauth.account.internal.flows.email_verification_by_code import EmailVerificationProcess
from allauth.account.internal.flows.password_reset_by_code import (
    PasswordResetVerificationProcess,
)
from allauth.account.models import EmailAddress
from allauth.core.internal import ratelimit
from allauth.headless.account.inputs import VerifyEmailInput
from django.contrib.auth.models import User
from django.core import mail
from django.db import connection
from django.test import Client, override_settings

from pyracast.accounts.inputs import ResetPasswordInput
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


def before(
    monkeypatch: pytest.MonkeyPatch, owner: type, name: str, competitor: Callable[[], object]
) -> None:
    original = getattr(owner, name)

    def run_competitor_first(self: Any, *args: Any, **kwargs: Any) -> Any:
        competitor()
        return original(self, *args, **kwargs)

    monkeypatch.setattr(owner, name, run_competitor_first)


def blocked_elsewhere(statement: str, *params: object) -> bool:
    database = connection.settings_dict
    with psycopg.connect(
        dbname=database["NAME"],
        user=database["USER"],
        password=database["PASSWORD"],
        host=database["HOST"],
        port=database["PORT"],
        autocommit=True,
    ) as other:
        other.execute("SET lock_timeout = '200ms'")
        try:
            other.execute(statement, params)
        except psycopg.errors.LockNotAvailable:
            return True
    return False


def verified_signup_address() -> None:
    EmailAddress.objects.filter(email="owner@example.com").update(verified=True)


def verified_change_address(user: User) -> Callable[[], object]:
    return lambda: EmailAddress.objects.get_or_create(
        user=user, email="new@example.com", defaults={"verified": True}
    )


def before_property(
    monkeypatch: pytest.MonkeyPatch, owner: type, name: str, competitor: Callable[[], object]
) -> None:
    original = getattr(owner, name)

    def run_competitor_first(self: Any) -> Any:
        competitor()
        return original.fget(self)

    monkeypatch.setattr(owner, name, property(run_competitor_first))


def test_signup_code_answers_like_nothing_pending_when_verified_before_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, code = pending_signup()
    expected = answer(submit(browser(), "auth/email/verify", {"key": code}))
    before(monkeypatch, VerifyEmailInput, "clean_key", verified_signup_address)

    response = submit(client, "auth/email/verify", {"key": code})

    assert answer(response) == expected
    assert mail.outbox == []
    assert_signed_out_without_pending_verification(client)
    assert EmailAddress.objects.get(email="owner@example.com").verified


def test_change_code_answers_like_nothing_pending_when_verified_before_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = member()
    client, code = change_pending(user)
    methods = client.get(AUTH + "auth/session").json()["data"]["methods"]
    expected = answer(submit(signed_in(user), "auth/email/verify", {"key": code}))
    before(monkeypatch, VerifyEmailInput, "clean_key", verified_change_address(user))

    response = submit(client, "auth/email/verify", {"key": code})

    assert answer(response) == expected
    assert mail.outbox == []
    assert client.get(AUTH + "auth/session").json()["data"]["methods"] == methods
    assert EmailAddress.objects.get(user=user, email="new@example.com").verified
    assert EmailAddress.objects.get(user=user, primary=True).email == "member@example.com"


def test_signup_resend_mails_nothing_when_verified_before_the_code_is_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = pending_signup()
    one_minute_later(monkeypatch)
    expected = answer(submit(browser(), "auth/email/verify/resend", {}))
    before_property(monkeypatch, EmailVerificationProcess, "can_resend", verified_signup_address)

    response = submit(client, "auth/email/verify/resend", {})

    assert answer(response) == expected
    assert mail.outbox == []
    assert_signed_out_without_pending_verification(client)
    assert EmailAddress.objects.get(email="owner@example.com").verified


def test_change_resend_mails_nothing_when_verified_before_the_code_is_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = member()
    client, _ = change_pending(user)
    methods = client.get(AUTH + "auth/session").json()["data"]["methods"]
    one_minute_later(monkeypatch)
    expected = answer(submit(signed_in(user), "auth/email/verify/resend", {}))
    before_property(
        monkeypatch, EmailVerificationProcess, "can_resend", verified_change_address(user)
    )

    response = submit(client, "auth/email/verify/resend", {})

    assert answer(response) == expected
    assert mail.outbox == []
    assert client.get(AUTH + "auth/session").json()["data"]["methods"] == methods
    assert EmailAddress.objects.get(user=user, email="new@example.com").verified


def test_reset_answers_like_nothing_pending_when_the_account_is_deleted_before_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = member()
    client = browser()
    assert submit(client, "auth/password/request", {"email": user.email}).status_code == 401
    reset = {"key": mailed_code(), "password": PASSWORD + "reset"}
    expected = answer(submit(browser(), "auth/password/reset", reset))
    before(
        monkeypatch,
        ResetPasswordInput,
        "clean",
        lambda: User.objects.filter(pk=user.pk).delete(),
    )

    response = submit(client, "auth/password/reset", reset)

    assert answer(response) == expected
    assert mail.outbox == []
    assert not User.objects.filter(pk=user.pk).exists()
    assert client.get("/api/session/").json()["user"] is None


def held_rows(email: str, user_id: int) -> tuple[bool, bool]:
    return (
        blocked_elsewhere("SELECT 1 FROM auth_user WHERE id = %s FOR UPDATE", user_id),
        blocked_elsewhere(
            "UPDATE account_emailaddress SET verified = true WHERE email = %s", email
        ),
    )


def test_signup_verification_holds_the_account_and_address_until_it_completes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, code = pending_signup()
    owner = User.objects.get(email="owner@example.com")
    held: list[tuple[bool, bool]] = []
    before(
        monkeypatch,
        EmailVerificationProcess,
        "finish",
        lambda: held.append(held_rows("owner@example.com", owner.pk)),
    )

    response = submit(client, "auth/email/verify", {"key": code})

    assert held == [(True, True)]
    assert response.status_code == 200
    assert EmailAddress.objects.get(email="owner@example.com").verified


def test_change_verification_holds_the_account_until_it_completes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = member()
    client, code = change_pending(user)
    held: list[bool] = []
    before(
        monkeypatch,
        EmailVerificationProcess,
        "finish",
        lambda: held.append(
            blocked_elsewhere("SELECT 1 FROM auth_user WHERE id = %s FOR UPDATE", user.pk)
        ),
    )

    response = submit(client, "auth/email/verify", {"key": code})

    assert held == [True]
    assert response.status_code == 200
    assert EmailAddress.objects.get(user=user, primary=True).email == "new@example.com"


def test_signup_resend_holds_the_account_and_address_until_the_code_is_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = pending_signup()
    owner = User.objects.get(email="owner@example.com")
    one_minute_later(monkeypatch)
    held: list[tuple[bool, bool]] = []
    before(
        monkeypatch,
        EmailVerificationProcess,
        "resend",
        lambda: held.append(held_rows("owner@example.com", owner.pk)),
    )

    response = submit(client, "auth/email/verify/resend", {})

    assert held == [(True, True)]
    assert response.status_code == 200
    assert mailed_code()


def test_change_resend_holds_the_account_until_the_code_is_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = member()
    client, _ = change_pending(user)
    one_minute_later(monkeypatch)
    held: list[bool] = []
    before(
        monkeypatch,
        EmailVerificationProcess,
        "resend",
        lambda: held.append(
            blocked_elsewhere("SELECT 1 FROM auth_user WHERE id = %s FOR UPDATE", user.pk)
        ),
    )

    response = submit(client, "auth/email/verify/resend", {})

    assert held == [True]
    assert response.status_code == 200
    assert mailed_code()


def test_reset_holds_the_account_until_it_completes(monkeypatch: pytest.MonkeyPatch) -> None:
    user = member()
    client = browser()
    assert submit(client, "auth/password/request", {"email": user.email}).status_code == 401
    reset = {"key": mailed_code(), "password": PASSWORD + "reset"}
    held: list[bool] = []
    before(
        monkeypatch,
        PasswordResetVerificationProcess,
        "finish",
        lambda: held.append(
            blocked_elsewhere("SELECT 1 FROM auth_user WHERE id = %s FOR UPDATE", user.pk)
        ),
    )

    response = submit(client, "auth/password/reset", reset)

    assert held == [True]
    assert response.status_code == 401
    user.refresh_from_db()
    assert user.check_password(PASSWORD + "reset")
