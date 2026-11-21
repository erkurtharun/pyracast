from collections.abc import Iterator
from typing import Any

import pytest
from django.db import DatabaseError
from django.test import Client, override_settings

from pyracast.accounts import session_cookie
from pyracast.accounts.models import OutboundMail
from tests.support.account_sessions import account, browser, session_key, session_row_held
from tests.support.auth_lifecycle import PASSWORD, submit
from tests.support.company_registration import signup

pytestmark = pytest.mark.django_db(databases=["default", "auth_cache"], transaction=True)


@pytest.fixture(autouse=True)
def local_mail() -> Iterator[None]:
    with override_settings(
        MAILERS={"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}}
    ):
        yield


def queued() -> list[str]:
    return list(OutboundMail.objects.values_list("kind", flat=True))


def with_session() -> Client:
    # A browser whose anonymous session is stored already, as after an earlier sign-in step.
    client = browser()
    session = client.session
    session["visited"] = True
    session.save()
    return client


def answered(response: Any) -> tuple[int, str]:
    return response.status_code, response.json()["code"]


def test_a_sign_in_code_whose_session_cannot_be_saved_is_not_queued() -> None:
    user = account("owner", verified=False)
    client = with_session()
    client.raise_request_exception = False

    with session_row_held(session_key(client)):
        response = submit(client, "auth/login", {"email": user.email, "password": PASSWORD})

    assert queued() == []
    assert answered(response) == (503, "busy")


def test_a_password_reset_code_whose_session_cannot_be_saved_is_not_queued() -> None:
    user = account("owner")
    client = with_session()
    client.raise_request_exception = False

    with session_row_held(session_key(client)):
        response = submit(client, "auth/password/request", {"email": user.email})

    assert queued() == []
    assert answered(response) == (503, "busy")


def test_a_sign_up_code_whose_new_session_cannot_be_stored_is_not_queued(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = browser()
    client.raise_request_exception = False

    def refused(_store: Any) -> None:
        raise DatabaseError("The session could not be stored.")

    monkeypatch.setattr(session_cookie.SessionStore, "create", refused)

    response = signup(client)

    assert response.status_code >= 500
    assert queued() == []


def test_a_sign_up_queues_its_code_with_the_session_that_holds_it() -> None:
    client = browser()

    response = signup(client)

    assert response.status_code == 401
    assert queued() == ["account/email/email_confirmation_signup"]
    assert session_key(client)
