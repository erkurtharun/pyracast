from typing import Any

import pytest
from allauth.account import signals as account_signals
from allauth.mfa import signals as mfa_signals
from allauth.usersessions.models import UserSession
from django.contrib.auth.models import User
from django.dispatch import Signal
from django.test import Client, RequestFactory

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize(
    "signal",
    [
        mfa_signals.authenticator_added,
        mfa_signals.authenticator_removed,
        mfa_signals.authenticator_reset,
        account_signals.email_changed,
    ],
)
def test_credential_change_ends_other_sessions_without_affecting_another_user(
    signal: Signal,
) -> None:
    actor = User.objects.create_user(username="owner")
    other = User.objects.create_user(username="other")
    current, stale, unrelated = Client(), Client(), Client()
    for client, user in ((current, actor), (stale, actor), (unrelated, other)):
        client.force_login(user)
        assert client.get("/api/session/").json()["user"]["id"] == user.pk
    assert UserSession.objects.filter(user=actor).count() == 2
    request = RequestFactory().post("/api/auth/browser/v1/account/authenticators/totp")
    request.user = actor
    request.session = current.session
    values: dict[str, Any] = {
        "request": request,
        "user": actor,
        "authenticator": None,
        "from_email_address": None,
        "to_email_address": None,
    }
    signal.send(sender=User, **values)
    assert stale.get("/api/session/").json()["user"] is None
    assert current.get("/api/session/").json()["user"]["id"] == actor.pk
    assert unrelated.get("/api/session/").json()["user"]["id"] == other.pk
    assert UserSession.objects.filter(user=actor).count() == 1
