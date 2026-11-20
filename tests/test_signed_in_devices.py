import time
from collections.abc import Iterator
from typing import Any

import pytest
from allauth.usersessions.models import UserSession
from django.contrib.sessions.backends.db import SessionStore
from django.db import connection
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext

from tests.support.account_sessions import (
    account,
    browser,
    capture_security_events,
    session_key,
    session_user,
    signed_in,
)
from tests.support.auth_lifecycle import AUTH

pytestmark = pytest.mark.django_db(databases=["default", "auth_cache"], transaction=True)

SESSIONS = AUTH + "auth/sessions"
FIELDS = {"id", "ip", "user_agent", "created_at", "last_seen_at", "is_current"}


@pytest.fixture(autouse=True)
def local_mail() -> Iterator[None]:
    with override_settings(
        MAILERS={"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}}
    ):
        yield


def token_of(client: Client) -> str:
    return str(client.get("/api/session/").json()["csrf_token"])


def listed(client: Client) -> list[dict[str, Any]]:
    response = client.get(SESSIONS)
    assert response.status_code == 200
    return list(response.json()["data"])


def end(client: Client, ids: list[Any], *, token: str | None = None) -> Any:
    return client.delete(
        SESSIONS,
        {"sessions": ids},
        content_type="application/json",
        HTTP_X_CSRFTOKEN=token_of(client) if token is None else token,
    )


def row_of(client: Client) -> UserSession:
    session: UserSession = UserSession.objects.get(session_key=session_key(client))
    return session


def test_the_list_shows_where_the_account_is_signed_in_and_nothing_of_other_accounts() -> None:
    owner, stranger = account("owner"), account("stranger")
    here, there = signed_in(owner), signed_in(owner)
    signed_in(stranger)

    sessions = listed(here)

    assert sorted(entry["id"] for entry in sessions) == sorted([row_of(here).pk, row_of(there).pk])
    assert all(set(entry) == FIELDS for entry in sessions)
    assert [entry["id"] for entry in sessions if entry["is_current"]] == [row_of(here).pk]


def test_a_session_past_its_idle_limit_is_not_listed() -> None:
    owner = account("owner")
    here, idle = signed_in(owner), signed_in(owner)
    store = SessionStore(session_key(idle))
    store["last_activity_at"] = time.time() - 31 * 60
    store.save()

    assert [entry["id"] for entry in listed(here)] == [row_of(here).pk]


def test_ending_another_session_of_the_account_signs_it_out(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = account("owner")
    here, there = signed_in(owner), signed_in(owner)
    ended = row_of(there).pk
    lines = capture_security_events(caplog, monkeypatch)

    response = end(here, [ended])

    assert response.status_code == 200
    assert [entry["id"] for entry in response.json()["data"]] == [row_of(here).pk]
    assert session_user(there) is None
    assert session_user(here) == owner.pk
    revoked = [line for line in lines() if line["event"] == "sessions_revoked"]
    assert [(line["reason"], line["count"]) for line in revoked] == [("ended_by_owner", 1)]


def test_ending_every_other_session_keeps_this_one() -> None:
    owner = account("owner")
    here, first, second = signed_in(owner), signed_in(owner), signed_in(owner)

    response = end(here, [row_of(first).pk, row_of(second).pk])

    assert response.status_code == 200
    assert session_user(first) is None
    assert session_user(second) is None
    assert session_user(here) == owner.pk


def test_ending_this_session_signs_this_browser_out() -> None:
    owner = account("owner")
    here = signed_in(owner)

    response = end(here, [row_of(here).pk])

    assert response.status_code == 401
    assert session_user(here) is None


def test_another_accounts_session_is_refused_like_one_that_does_not_exist() -> None:
    owner, stranger = account("owner"), account("stranger")
    here, theirs = signed_in(owner), signed_in(stranger)
    foreign = row_of(theirs).pk
    missing = UserSession.objects.order_by("-pk").values_list("pk", flat=True)[0] + 1000

    refused_foreign = end(here, [foreign])
    refused_missing = end(here, [missing])

    assert refused_foreign.status_code == refused_missing.status_code == 400
    assert refused_foreign.json() == refused_missing.json()
    assert session_user(theirs) == stranger.pk
    assert session_user(here) == owner.pk


def test_ending_sessions_needs_the_form_token() -> None:
    owner = account("owner")
    here, there = signed_in(owner), signed_in(owner)

    response = end(here, [row_of(there).pk], token="")

    assert (response.status_code, response.json()["code"]) == (403, "csrf_failed")
    assert session_user(there) == owner.pk


def test_the_session_list_is_rate_limited() -> None:
    owner = account("owner")
    here = signed_in(owner)

    with override_settings(ACCOUNT_RATE_LIMITS={"manage_sessions": "2/m/user"}):
        answers = [here.get(SESSIONS).status_code for _ in range(3)]

    assert answers == [200, 200, 429]


def test_a_signed_out_browser_lists_nothing() -> None:
    response = browser().get(SESSIONS)

    assert response.status_code == 401


def test_ending_a_session_takes_the_account_before_the_session_rows() -> None:
    owner = account("owner")
    here, there = signed_in(owner), signed_in(owner)
    token = token_of(here)

    with CaptureQueriesContext(connection) as queries:
        assert end(here, [row_of(there).pk], token=token).status_code == 200

    statements = [query["sql"] for query in queries.captured_queries]
    account_lock = next(
        index
        for index, sql in enumerate(statements)
        if 'FROM "auth_user"' in sql and "FOR NO KEY UPDATE" in sql
    )
    session_delete = next(
        index
        for index, sql in enumerate(statements)
        if sql.startswith('DELETE FROM "django_session"')
    )
    assert account_lock < session_delete
