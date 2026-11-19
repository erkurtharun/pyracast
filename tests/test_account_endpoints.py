from collections.abc import Iterator

import pytest
from django.core import mail
from django.test import override_settings

from tests.support.account_sessions import account, browser, signed_in
from tests.support.auth_lifecycle import AUTH, submit

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])


@pytest.fixture(autouse=True)
def local_mail() -> Iterator[None]:
    with override_settings(
        MAILERS={"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}}
    ):
        yield


@pytest.mark.parametrize("method", ["put", "patch"])
def test_the_account_email_endpoint_answers_only_reads_changes_and_removals(method: str) -> None:
    client = signed_in(account("owner"), reauthenticated=True)
    token = client.get("/api/session/").json()["csrf_token"]

    response = getattr(client, method)(
        AUTH + "account/email",
        {"email": "owner@example.com", "primary": True},
        content_type="application/json",
        HTTP_X_CSRFTOKEN=token,
    )

    assert (response.status_code, response.json()["code"]) == (405, "method_not_allowed")
    assert mail.outbox == []


def test_address_change_requests_are_limited_per_account() -> None:
    client = signed_in(account("owner"), reauthenticated=True)

    statuses = [
        submit(client, "account/email", {"email": f"new-{index}@example.com"}).status_code
        for index in range(4)
    ]

    assert statuses == [200, 200, 200, 429]
    assert len(mail.outbox) == 3


@pytest.mark.parametrize("method", ["get", "put", "patch", "delete"])
def test_the_password_reset_request_route_answers_only_posts(method: str) -> None:
    client = browser()
    token = client.get("/api/session/").json()["csrf_token"]

    response = getattr(client, method)(
        AUTH + "auth/password/request",
        {"email": "owner@example.com"},
        content_type="application/json",
        HTTP_X_CSRFTOKEN=token,
    )

    assert (response.status_code, response.json()["code"]) == (405, "method_not_allowed")
    assert mail.outbox == []


@pytest.mark.parametrize(
    ("password", "status"), [("aB!57mQ8nP4zK2w", 401), ("aB!57mQ8nP4zK2", 400)]
)
def test_a_signup_password_needs_fifteen_characters(password: str, status: int) -> None:
    signup = {"email": "new@example.com", "password": password, "company_name": "Solar"}

    response = submit(browser(), "auth/signup", signup)

    assert response.status_code == status
    if status == 400:
        assert [error["code"] for error in response.json()["errors"]] == ["password_too_short"]
