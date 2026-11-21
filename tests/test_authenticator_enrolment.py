import time
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from allauth.account.internal.flows import reauthentication
from allauth.account.internal.flows.login import AUTHENTICATION_METHODS_SESSION_KEY
from allauth.account.models import EmailAddress
from allauth.core.exceptions import ReauthenticationRequired
from allauth.mfa import signals as mfa_signals
from allauth.mfa.models import Authenticator
from allauth.mfa.recovery_codes.internal.auth import RecoveryCodes
from allauth.mfa.totp.internal.auth import generate_totp_secret
from django.contrib.auth.models import User
from django.contrib.sessions.backends.db import SessionStore
from django.test import Client, RequestFactory, override_settings

from pyracast.accounts import middleware, processes, security, sessions
from pyracast.accounts.mfa_views import ManageTOTPView
from tests.support.account_sessions import account, session_key, signed_in, totp_clock
from tests.support.auth_lifecycle import AUTH, PASSWORD, submit, totp_code
from tests.support.step_up import holds_current_authority

pytestmark = pytest.mark.django_db(databases=["default", "auth_cache"], transaction=True)

TOTP = "account/authenticators/totp"
CODES = "account/authenticators/recovery-codes"
SETUP_KEY = "mfa.totp.secret"
SECOND = "second@example.com"


@pytest.fixture(autouse=True)
def local_mail() -> Iterator[None]:
    with override_settings(
        MAILERS={"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}}
    ):
        yield


@pytest.fixture
def now(monkeypatch: pytest.MonkeyPatch) -> float:
    moment = time.time()
    totp_clock(monkeypatch, moment)
    return moment


def confirmed(user: User) -> Client:
    return signed_in(user, reauthenticated=True)


def setup_secret(client: Client) -> str:
    response = client.get(AUTH + TOTP)
    assert response.status_code == 404
    return str(response.json()["meta"]["secret"])


def activate(client: Client, secret: str, now: float) -> Any:
    return submit(client, TOTP, {"code": totp_code(secret, now)})


def age_confirmations(client: Client, seconds: float) -> None:
    store = SessionStore(session_key(client))
    records = store[AUTHENTICATION_METHODS_SESSION_KEY]
    for record in records:
        record["at"] -= seconds
    store[AUTHENTICATION_METHODS_SESSION_KEY] = records
    store.save()


def confirmation_times(client: Client) -> list[float]:
    records = SessionStore(session_key(client))[AUTHENTICATION_METHODS_SESSION_KEY]
    return [record["at"] for record in records if record.get("reauthenticated")]


def test_an_authenticator_is_enrolled_with_one_confirmation_and_its_codes_come_with_it(
    now: float,
) -> None:
    user = account("owner")
    client = confirmed(user)

    activation = activate(client, setup_secret(client), now)

    assert activation.status_code == 200
    codes = activation.json()["meta"]["recovery_codes"]
    assert len(codes) == 10
    assert Authenticator.objects.filter(user=user, type=Authenticator.Type.TOTP).exists()
    replaced = submit(client, CODES, {})
    assert replaced.status_code == 200
    assert set(replaced.json()["data"]["unused_codes"]).isdisjoint(codes)


def test_the_setup_secret_is_issued_only_after_a_confirmation() -> None:
    user = account("owner")
    client = signed_in(user)

    refused = client.get(AUTH + TOTP)

    assert refused.status_code == 401
    assert [flow["id"] for flow in refused.json()["data"]["flows"]] == ["reauthenticate"]
    assert SETUP_KEY not in SessionStore(session_key(client))


def test_an_activation_asks_for_the_confirmation_before_it_reads_the_code(now: float) -> None:
    user = account("owner")
    client = signed_in(user)

    refused = submit(client, TOTP, {"code": "000000"})

    assert refused.status_code == 401
    assert [flow["id"] for flow in refused.json()["data"]["flows"]] == ["reauthenticate"]
    assert not Authenticator.objects.filter(user=user).exists()


def test_a_code_for_a_secret_the_session_was_never_given_activates_nothing(now: float) -> None:
    user = account("owner")
    client = confirmed(user)

    response = activate(client, generate_totp_secret(), now)

    assert response.status_code == 400
    assert not Authenticator.objects.filter(user=user).exists()


def test_the_codes_of_an_activation_are_shown_once(now: float) -> None:
    user = account("owner")
    client = confirmed(user)
    assert activate(client, setup_secret(client), now).status_code == 200

    shown = client.get(AUTH + CODES)

    assert shown.status_code == 200
    assert "unused_codes" not in shown.json()["data"]
    assert shown.json()["data"]["unused_code_count"] == 10
    assert client.get(AUTH + TOTP).json()["data"]["type"] == "totp"


def test_an_activation_shows_no_recovery_codes_it_did_not_create(now: float) -> None:
    user = account("owner")
    existing = RecoveryCodes.activate(user).get_unused_codes()
    client = signed_in(user)
    # Recovery codes are the account's strongest factor, so one of them confirms the session.
    assert submit(client, "auth/2fa/reauthenticate", {"code": existing[0]}).status_code == 200

    activation = activate(client, setup_secret(client), now)

    assert activation.status_code == 200
    assert "meta" not in activation.json()


def test_a_sessions_own_change_keeps_its_authority_for_the_window_of_its_confirmation(
    now: float,
) -> None:
    user = account("owner")
    client = confirmed(user)
    assert activate(client, setup_secret(client), now).status_code == 200

    assert holds_current_authority(client, user.pk)
    age_confirmations(client, 301)
    refused = submit(client, CODES, {})
    assert refused.status_code == 401
    assert [flow["id"] for flow in refused.json()["data"]["flows"]] == ["mfa_reauthenticate"]


def test_an_activation_judged_inside_its_confirmations_window_is_answered_with_that_window(
    now: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = account("owner")
    client = confirmed(user)
    secret = setup_secret(client)
    confirmed_at = confirmation_times(client)[0]
    judgments: list[float] = []

    def judge() -> float:
        judgments.append(confirmed_at + (301 if judgments else 299))
        return judgments[-1]

    def later() -> float:
        return confirmed_at + (301 if judgments else 299)

    monkeypatch.setattr(reauthentication, "time", SimpleNamespace(time=judge))
    for clocked in (processes, security, sessions, middleware):
        monkeypatch.setattr(clocked, "time", SimpleNamespace(time=later))
    added: list[str] = []

    def record_added(authenticator: Authenticator, **_kwargs: Any) -> None:
        added.append(authenticator.type)

    mfa_signals.authenticator_added.connect(record_added)
    try:
        activation = activate(client, secret, now)
    finally:
        mfa_signals.authenticator_added.disconnect(record_added)

    assert judgments == [confirmed_at + 299]
    assert activation.status_code == 200
    assert len(activation.json()["meta"]["recovery_codes"]) == 10
    assert confirmation_times(client) == [confirmed_at, confirmed_at]
    kinds = {Authenticator.Type.TOTP, Authenticator.Type.RECOVERY_CODES}
    assert set(Authenticator.objects.filter(user=user).values_list("type", flat=True)) == kinds
    assert sorted(added) == sorted(kinds)
    assert SETUP_KEY not in SessionStore(session_key(client))


def test_only_the_session_that_made_a_change_keeps_its_authority() -> None:
    user = account("owner")
    EmailAddress.objects.create(user=user, email=SECOND, verified=True)
    this, other = confirmed(user), confirmed(user)
    token = other.get("/api/session/").json()["csrf_token"]
    removed = other.delete(
        AUTH + "account/email",
        {"email": SECOND},
        content_type="application/json",
        HTTP_X_CSRFTOKEN=token,
    )
    assert removed.status_code == 200

    assert holds_current_authority(other, user.pk)
    assert not holds_current_authority(this, user.pk)
    change = {"current_password": PASSWORD, "new_password": PASSWORD + "-changed"}
    refused = submit(this, "account/password/change", change)
    assert refused.status_code == 401
    assert User.objects.get(pk=user.pk).check_password(PASSWORD)


def test_the_activation_handler_refuses_a_request_without_a_judged_confirmation() -> None:
    user = account("owner")
    request = RequestFactory().post("/")
    request.user = user
    view = ManageTOTPView()

    with pytest.raises(ReauthenticationRequired):
        view.post(request)

    assert not Authenticator.objects.filter(user=user).exists()
