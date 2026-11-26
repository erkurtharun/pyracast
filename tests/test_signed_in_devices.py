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


@pytest.mark.parametrize(
    ("idle_seconds", "listed_too"),
    [(31 * 60, True), (30 * 24 * 60 * 60 + 60, False)],
    ids=["half-an-hour", "past-thirty-days"],
)
def test_a_member_session_is_listed_until_its_idle_limit_of_thirty_days(
    idle_seconds: int, listed_too: bool
) -> None:
    owner = account("owner")
    here, idle = signed_in(owner), signed_in(owner)
    store = SessionStore(session_key(idle))
    store["last_activity_at"] = time.time() - idle_seconds
    store.save()
    expected = [row_of(here).pk, row_of(idle).pk] if listed_too else [row_of(here).pk]

    assert sorted(entry["id"] for entry in listed(here)) == sorted(expected)


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


def test_another_accounts_session_is_left_like_one_that_does_not_exist() -> None:
    owner, stranger = account("owner"), account("stranger")
    here, theirs = signed_in(owner), signed_in(stranger)
    foreign = row_of(theirs).pk
    missing = UserSession.objects.order_by("-pk").values_list("pk", flat=True)[0] + 1000

    with_foreign = end(here, [foreign])
    with_missing = end(here, [missing])

    assert with_foreign.status_code == with_missing.status_code == 200
    assert with_foreign.json() == with_missing.json()
    assert [entry["id"] for entry in with_foreign.json()["data"]] == [row_of(here).pk]
    assert session_user(theirs) == stranger.pk
    assert session_user(here) == owner.pk


def test_sessions_that_ended_meanwhile_leave_the_named_ones_that_remain_to_end() -> None:
    owner, stranger = account("owner"), account("stranger")
    here, first, second = signed_in(owner), signed_in(owner), signed_in(owner)
    theirs = signed_in(stranger)
    ended, remaining = row_of(first).pk, row_of(second).pk
    assert end(here, [ended]).status_code == 200

    response = end(here, [ended, remaining, row_of(theirs).pk])

    assert response.status_code == 200
    assert [entry["id"] for entry in response.json()["data"]] == [row_of(here).pk]
    assert session_user(second) is None
    assert session_user(theirs) == stranger.pk
    assert session_user(here) == owner.pk


@pytest.mark.parametrize(
    "ids",
    [[], ["first"], "2", None, ["2"], [None], [True], [False], [1.5], [2**63], [10**30]],
    ids=repr,
)
def test_a_selection_that_is_not_a_list_of_session_ids_is_refused(ids: Any) -> None:
    owner = account("owner")
    here, there = signed_in(owner), signed_in(owner)

    response = end(here, ids)

    assert response.status_code == 400
    assert response.json()["code"] == "invalid_request"
    assert session_user(there) == owner.pk
    assert session_user(here) == owner.pk


def selection_reads(statements: list[str]) -> list[str]:
    return [sql for sql in statements if '"usersessions_usersession"."id" IN' in sql]


def test_unknown_and_foreign_sessions_read_the_selection_once_and_answer_alike() -> None:
    owner, stranger = account("owner"), account("stranger")
    here, theirs = signed_in(owner), signed_in(stranger)
    foreign = row_of(theirs).pk
    missing = UserSession.objects.order_by("-pk").values_list("pk", flat=True)[0] + 1000
    token = token_of(here)

    def ending(ids: list[int]) -> tuple[Any, list[str]]:
        with CaptureQueriesContext(connection) as queries:
            response = end(here, ids, token=token)
        return response, [query["sql"] for query in queries.captured_queries]

    with_foreign, foreign_statements = ending([foreign])
    with_missing, missing_statements = ending([missing])

    assert with_foreign.status_code == with_missing.status_code == 200
    assert with_foreign.json() == with_missing.json()
    assert len(foreign_statements) == len(missing_statements)
    assert len(selection_reads(foreign_statements)) == 1
    assert len(selection_reads(missing_statements)) == 1
    assert session_user(theirs) == stranger.pk


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
