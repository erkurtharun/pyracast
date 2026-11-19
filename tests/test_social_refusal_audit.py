import json
import logging
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from allauth.socialaccount.models import SocialAccount, SocialApp, SocialLogin
from allauth.socialaccount.providers.google.provider import GoogleProvider
from django.contrib.auth.models import AnonymousUser, User
from django.core.exceptions import PermissionDenied
from django.test import Client, RequestFactory, override_settings

from pyracast.accounts.audit import logger, refusal_throttle
from pyracast.accounts.social import (
    SocialAccountAdapter,
    authorize_connection,
    record_social_refusal,
)
from tests.support.auth_lifecycle import PASSWORD, member, submit
from tests.support.social_accounts import (
    CALLBACK,
    PROVIDERS,
    begin,
    complete,
    serve_provider_profile,
)

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])


@pytest.fixture(autouse=True)
def social_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    serve_provider_profile(monkeypatch)
    with override_settings(
        SOCIALACCOUNT_PROVIDERS=PROVIDERS,
    ):
        yield


@pytest.fixture
def refusals(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> Callable[[], list[dict[str, Any]]]:
    monkeypatch.setattr(logger, "handlers", [caplog.handler])
    monkeypatch.setattr(logger, "propagate", False)
    monkeypatch.setattr(refusal_throttle, "_seen", OrderedDict())
    caplog.set_level(logging.INFO, logger="pyracast.security")

    def lines() -> list[dict[str, Any]]:
        events = [
            json.loads(record.getMessage())
            for record in caplog.records
            if record.name == "pyracast.security"
        ]
        return [event for event in events if event["event"] == "social_login_refused"]

    return lines


@pytest.mark.parametrize(
    "failure", ["origin_missing", "user_changed", "logged_out", "reauth_missing", "reauth_expired"]
)
def test_a_refused_connection_records_one_line_for_its_current_caller(
    failure: str, refusals: Callable[[], list[dict[str, Any]]]
) -> None:
    user = member()
    client = Client(enforce_csrf_checks=True)
    assert (
        submit(client, "auth/login", {"email": user.email, "password": PASSWORD}).status_code == 200
    )
    assert submit(client, "auth/reauthenticate", {"password": PASSWORD}).status_code == 200
    state = begin(client, process="connect")
    actor_id: int | None = user.pk
    if failure in {"user_changed", "logged_out"}:
        states = client.session["socialaccount_states"]
        if failure == "user_changed":
            other = User.objects.create_user(username="other", password=PASSWORD)
            client.force_login(other)
            actor_id = other.pk
        else:
            client.logout()
            actor_id = None
        session = client.session
        session["socialaccount_states"] = states
    else:
        session = client.session
        if failure == "origin_missing":
            states = session["socialaccount_states"]
            states[state][0]["data"] = {}
            session["socialaccount_states"] = states
        else:
            records = session["account_authentication_methods"]
            for record in records:
                if failure == "reauth_expired":
                    record["at"] = time.time() - 600
                else:
                    record.pop("reauthenticated", None)
            session["account_authentication_methods"] = records
    session.save()

    response = complete(client, state)

    assert response.status_code == 302
    assert "error=permission_denied" in response["Location"]
    assert not SocialAccount.objects.exists()
    [line] = refusals()
    assert line["user_id"] == actor_id
    assert line["provider"] == "google"
    assert line["reason"] == (
        "initiating_user_missing" if failure == "origin_missing" else "reauthentication_required"
    )


@pytest.mark.parametrize("signed_in", [False, True])
def test_a_disabled_provider_callback_records_its_authenticated_caller(
    signed_in: bool, refusals: Callable[[], list[dict[str, Any]]]
) -> None:
    client = Client(enforce_csrf_checks=True)
    actor_id = None
    if signed_in:
        user = member()
        assert (
            submit(client, "auth/login", {"email": user.email, "password": PASSWORD}).status_code
            == 200
        )
        actor_id = user.pk

    with override_settings(SOCIALACCOUNT_PROVIDERS={}):
        response = client.get("/api/accounts/google/login/callback/")

    assert response.status_code == 302
    assert response["Location"] == f"{CALLBACK}&error=provider_unavailable"
    [line] = refusals()
    assert line["user_id"] == actor_id
    assert line["provider"] == "google"
    assert line["reason"] == "provider_disabled"


def test_social_refusal_lines_are_throttled_separately_by_actor_provider_and_reason(
    refusals: Callable[[], list[dict[str, Any]]],
) -> None:
    request = RequestFactory().get("/api/accounts/google/login/callback/")
    first, second = User(pk=31), User(pk=32)
    cases = [
        (first, "google", "inactive"),
        (second, "google", "inactive"),
        (first, "microsoft", "inactive"),
        (first, "google", "mfa_not_provisioned"),
        (None, "google", "inactive"),
    ]
    for actor, provider, reason in cases:
        for _ in range(2):
            record_social_refusal(request, provider, reason, actor)

    assert [(line["user_id"], line["provider"], line["reason"]) for line in refusals()] == [
        (None if actor is None else actor.pk, provider, reason) for actor, provider, reason in cases
    ]


def test_a_reported_connection_refusal_is_not_recorded_again_as_a_provider_error(
    refusals: Callable[[], list[dict[str, Any]]],
) -> None:
    request = RequestFactory().get("/api/accounts/google/login/callback/")
    request.user = AnonymousUser()
    adapter = SocialAccountAdapter(request)
    login = SocialLogin(account=SocialAccount(provider="google", uid="provider-identity"))
    with pytest.raises(PermissionDenied) as denied:
        authorize_connection(request, login)

    provider = GoogleProvider(request, app=SocialApp(provider="google"))
    adapter.on_authentication_error(request, provider, exception=denied.value)

    [line] = refusals()
    assert line["reason"] == "initiating_user_missing"
    assert line["provider"] == "google"
    assert line["user_id"] is None
