import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator
from concurrent.futures import Future
from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from allauth.account import app_settings as account_settings
from allauth.account.internal.flows import reauthentication
from allauth.account.internal.flows.login import AUTHENTICATION_METHODS_SESSION_KEY
from allauth.account.models import EmailAddress
from allauth.mfa import signals as mfa_signals
from allauth.mfa.models import Authenticator
from allauth.mfa.recovery_codes.internal import flows as recovery_code_flows
from allauth.mfa.recovery_codes.internal.auth import RecoveryCodes
from allauth.mfa.totp.internal import auth as totp_auth
from allauth.mfa.totp.internal import flows as totp_flows
from allauth.mfa.totp.internal.auth import TOTP, generate_totp_secret
from allauth.socialaccount.internal import statekit
from allauth.socialaccount.models import SocialAccount
from django.contrib.auth.hashers import make_password
from django.contrib.auth.models import User
from django.core import mail
from django.db import DatabaseError, connection, transaction
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext

from pyracast.accounts import adapters, processes, security, social_views, step_up
from pyracast.accounts import middleware as account_middleware
from pyracast.accounts.audit import refusal_throttle
from pyracast.accounts.models import OutboundMail
from pyracast.accounts.sessions import end_sessions
from tests.support import account_sessions
from tests.support.account_sessions import (
    alongside,
    arrive,
    browser,
    capture_security_events,
    carrying_key,
    elsewhere,
    lasting_session_cookie,
    moment,
    session_cookie,
    session_key,
    session_row_held,
    session_user,
)
from tests.support.admin_mfa_bootstrap import run
from tests.support.auth_lifecycle import AUTH, PASSWORD, mailed_code, submit, totp_code
from tests.support.outbox import deliver_mail
from tests.support.social_accounts import (
    CALLBACK,
    PROVIDERS,
    begin,
    callback,
    complete,
    finish,
    provider_destination,
    serve_provider_profile,
)
from tests.support.step_up import FLOWS, STRONGEST, call, holder, proofs, signed_in, with_proof

pytestmark = pytest.mark.django_db(databases=["default", "auth_cache"], transaction=True)


@pytest.fixture(autouse=True)
def local_mail() -> Iterator[None]:
    with override_settings(
        MAILERS={"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}}
    ):
        yield


@pytest.fixture
def now(monkeypatch: pytest.MonkeyPatch) -> float:
    moment = time.time()
    monkeypatch.setattr(totp_auth, "time", SimpleNamespace(time=lambda: moment))
    return moment


def mailed_to() -> list[list[str]]:
    deliver_mail()
    return [message.to for message in mail.outbox]


def reauthenticate(client: Client, user: User, factor: str, code: str) -> int:
    if factor == "password":
        return int(call(client, "post", "auth/reauthenticate", {"password": PASSWORD}).status_code)
    if factor == "mfa":
        return int(call(client, "post", "auth/2fa/reauthenticate", {"code": code}).status_code)
    requested = call(client, "post", "auth/reauthenticate/code/request", {"email": user.email})
    if requested.status_code != 200:
        return int(requested.status_code)
    confirmed = call(client, "post", "auth/reauthenticate/code/confirm", {"code": mailed_code()})
    return int(confirmed.status_code)


@pytest.mark.parametrize("factor", ["mfa", "password", "code"])
@pytest.mark.parametrize("kind", list(STRONGEST))
def test_reauthentication_accepts_only_the_strongest_factor_the_account_holds(
    kind: str, factor: str, now: float
) -> None:
    user, secret = holder(kind)
    client = signed_in(user)
    code = totp_code(secret, now) if secret else "000000"

    status = reauthenticate(client, user, factor, code)

    if factor == STRONGEST[kind]:
        assert status == 200
        assert [proof["method"] for proof in proofs(client)] == [factor]
    else:
        assert status == 403
        assert proofs(client) == []
        assert mailed_to() == []


CHANGED_PASSWORD = PASSWORD + "-changed"
ACTIONS = {
    "add-address": ("post", "account/email", {"email": "added@example.com"}),
    "set-password": ("post", "account/password/change", {"new_password": PASSWORD + "-set"}),
    "change-password": (
        "post",
        "account/password/change",
        {"current_password": PASSWORD, "new_password": CHANGED_PASSWORD},
    ),
    "recovery-codes": ("get", "account/authenticators/recovery-codes", None),
    "remove-authenticator": ("delete", "account/authenticators/totp", None),
}
ACCOUNTS_OF = {
    "add-address": set(STRONGEST),
    "set-password": {"code", "authenticator-without-password"},
    "change-password": {"authenticator"},
    "recovery-codes": {"authenticator", "authenticator-without-password"},
    "remove-authenticator": {"authenticator", "authenticator-without-password"},
}
STEP_UPS = [(action, kind) for action, kinds in ACCOUNTS_OF.items() for kind in sorted(kinds)]
WEAKEST_FIRST = ["code", "password", "mfa"]
RETAINED = [
    (action, kind, factor)
    for action, kind in STEP_UPS
    for factor in WEAKEST_FIRST[: WEAKEST_FIRST.index(STRONGEST[kind])]
]


def stored(user: User) -> tuple[str, list[str], int]:
    return (
        User.objects.get(pk=user.pk).password,
        list(EmailAddress.objects.order_by("email").values_list("email", flat=True)),
        user.authenticator_set.count(),
    )


@pytest.mark.parametrize(("action", "kind"), STEP_UPS)
def test_a_step_up_asks_for_the_strongest_factor_the_account_holds_and_nothing_else(
    action: str, kind: str
) -> None:
    user, _secret = holder(kind)
    client = signed_in(user)
    before = stored(user)

    response = call(client, *ACTIONS[action])

    assert response.status_code == 401
    body = response.json()
    factor = STRONGEST[kind]
    assert [flow["id"] for flow in body["data"]["flows"]] == [FLOWS[factor]]
    assert body["meta"]["is_authenticated"] is True
    assert [error["code"] for error in body["errors"]] == [
        "mfa_reauthentication_required" if factor == "mfa" else "reauthentication_required"
    ]
    assert body["code"] == "not_authenticated"
    assert body["message"] == body["errors"][0]["message"]
    assert stored(user) == before
    assert mailed_to() == []


@pytest.mark.parametrize(("action", "kind", "factor"), RETAINED)
def test_a_retained_proof_by_a_weaker_factor_ends_and_the_step_up_asks_for_the_strongest(
    action: str, kind: str, factor: str
) -> None:
    user, _secret = holder(kind)
    client = signed_in(user)
    with_proof(client, user, factor, age=0, reauthenticated=True, retained=True)
    before = stored(user)

    response = call(client, *ACTIONS[action])

    assert response.status_code == 401
    assert [flow["id"] for flow in response.json()["data"]["flows"]] == [FLOWS[STRONGEST[kind]]]
    assert stored(user) == before
    assert proofs(client) == []
    assert mailed_to() == []


def test_the_session_that_adds_an_authenticator_keeps_its_confirmation_for_the_same_window(
    now: float,
) -> None:
    user, _secret = holder("password")
    client = signed_in(user)
    assert reauthenticate(client, user, "password", "") == 200
    window = [record["at"] for record in proofs(client)]
    secret = client.get(AUTH + "account/authenticators/totp").json()["meta"]["secret"]
    activation = {"code": totp_code(secret, now)}
    assert call(client, "post", "account/authenticators/totp", activation).status_code == 200

    response = call(client, *ACTIONS["add-address"])

    assert response.status_code == 200
    assert {record["at"] for record in proofs(client)} == set(window)
    assert [record["method"] for record in proofs(client)] == ["password", "mfa"]


def test_a_connection_needs_a_proof_by_the_strongest_factor() -> None:
    user, _secret = holder("authenticator")
    client = signed_in(user)
    with_proof(client, user, "password", age=0, reauthenticated=True, retained=True)
    token = client.get("/api/session/").json()["csrf_token"]

    response = client.post(
        AUTH + "auth/provider/redirect",
        {
            "provider": "google",
            "process": "connect",
            "callback_url": CALLBACK,
        },
        HTTP_X_CSRFTOKEN=token,
    )

    assert response.status_code == 302
    assert "error=reauthentication_required" in response["Location"]


def password_set(user: User) -> None:
    user.set_password(PASSWORD + "-elsewhere")
    user.save(update_fields=["password"])


def factor_added(user: User) -> None:
    if user.authenticator_set.filter(type="totp").exists():
        RecoveryCodes.activate(user)
    else:
        TOTP.activate(user, generate_totp_secret())


def factor_removed(user: User) -> None:
    user.authenticator_set.filter(type="recovery_codes").delete()


def factor_reset(user: User) -> None:
    factor_removed(user)
    RecoveryCodes.activate(user)


def address_replaced(user: User) -> None:
    EmailAddress.objects.filter(user=user).update(email="replaced@example.com")


# Credential changes made outside the session that holds the step. A password change ends every
# session of the account (the session hash and ACCOUNT_LOGOUT_ON_PASSWORD_CHANGE); the others
# keep the session, so only the step can end.
CHANGES_OF: dict[str, dict[str, Callable[[User], None]]] = {
    "code": {"password-set": password_set, "factor-added": factor_added},
    "password": {"password-changed": password_set, "factor-added": factor_added},
    "authenticator": {
        "password-changed": password_set,
        "factor-removed": factor_removed,
        "factor-reset": factor_reset,
    },
}
for changes in CHANGES_OF.values():
    changes["address-replaced"] = address_replaced
SESSION_ENDING = {"password-set", "password-changed"}
PROVEN_ACTIONS = {
    "code": ["add-address"],
    "password": ["add-address"],
    "authenticator": ["add-address", "change-password"],
}
PROOF_CHANGES = [
    (kind, action, change)
    for kind, changes in CHANGES_OF.items()
    for action in PROVEN_ACTIONS[kind]
    for change in ["unchanged", *changes]
]


def proven(kind: str, now: float) -> tuple[User, Client]:
    user, secret = holder(kind)
    if secret is not None:
        RecoveryCodes.activate(user)
    client = signed_in(user)
    code = totp_code(secret, now) if secret else ""
    assert reauthenticate(client, user, STRONGEST[kind], code) == 200
    assert len(proofs(client)) == 1
    deliver_mail()
    mail.outbox.clear()
    return user, client


def strongest_now(user: User) -> str:
    current = User.objects.get(pk=user.pk)
    if current.authenticator_set.exists():
        return "mfa"
    return "password" if current.has_usable_password() else "code"


@pytest.mark.parametrize(("kind", "action", "change"), PROOF_CHANGES)
def test_a_step_up_proof_holds_only_while_the_credential_state_it_was_given_under_holds(
    kind: str, action: str, change: str, now: float
) -> None:
    user, client = proven(kind, now)
    if change != "unchanged":
        CHANGES_OF[kind][change](user)
    before = stored(user)

    response = call(client, *ACTIONS[action])

    if change == "unchanged":
        if action == "add-address":
            assert response.status_code == 200
            assert mailed_to() == [["added@example.com"]]
        else:
            assert User.objects.get(pk=user.pk).check_password(CHANGED_PASSWORD)
        return
    assert stored(user) == before
    assert mailed_to() == []
    if change in SESSION_ENDING:
        signed_out = call(Client(enforce_csrf_checks=True), *ACTIONS[action])
        assert (response.status_code, response.json()) == (
            signed_out.status_code,
            signed_out.json(),
        )
        assert session_user(client) is None
        return
    assert response.status_code == 401
    assert [flow["id"] for flow in response.json()["data"]["flows"]] == [FLOWS[strongest_now(user)]]
    assert proofs(client) == []
    assert session_user(client) == user.pk


@pytest.mark.parametrize("change", ["unchanged", *CHANGES_OF["code"]])
def test_a_reauthentication_code_completes_only_under_the_state_it_was_sent_under(
    change: str,
) -> None:
    user, _secret = holder("code")
    client = signed_in(user)
    requested = call(client, "post", "auth/reauthenticate/code/request", {"email": user.email})
    assert requested.status_code == 200
    data = {"code": mailed_code()}
    mail.outbox.clear()
    if change == "unchanged":
        assert call(client, "post", "auth/reauthenticate/code/confirm", data).status_code == 200
        assert [proof["method"] for proof in proofs(client)] == ["code"]
        return
    CHANGES_OF["code"][change](user)
    nothing_pending = call(signed_in(user), "post", "auth/reauthenticate/code/confirm", data)

    response = call(client, "post", "auth/reauthenticate/code/confirm", data)

    assert proofs(client) == []
    assert mailed_to() == []
    if change in SESSION_ENDING:
        assert response.status_code == 401
        assert session_user(client) is None
        return
    assert (response.status_code, response.json()) == (
        nothing_pending.status_code,
        nothing_pending.json(),
    )
    assert session_user(client) == user.pk


def methods(client: Client) -> list[str]:
    records = client.session.get(AUTHENTICATION_METHODS_SESSION_KEY) or []
    return [record["method"] for record in records]


def test_an_ended_proof_leaves_the_session_and_its_records_in_place(now: float) -> None:
    user, client = proven("authenticator", now)
    recorded = methods(client)
    factor_removed(user)

    assert call(client, "get", "account/authenticators").status_code == 200

    assert proofs(client) == []
    assert methods(client) == recorded
    assert session_user(client) == user.pk


def test_a_session_without_a_proof_is_left_as_it_is() -> None:
    user, _secret = holder("authenticator")
    client = signed_in(user)
    with_proof(client, user, "mfa", age=0, reauthenticated=False, type="totp", id=1)
    recorded = client.session[AUTHENTICATION_METHODS_SESSION_KEY]
    address_replaced(user)

    assert call(client, "get", "account/authenticators").status_code == 200

    assert client.session[AUTHENTICATION_METHODS_SESSION_KEY] == recorded
    assert session_user(client) == user.pk


@pytest.mark.parametrize("action", ["add-address", "change-password"])
def test_a_proof_by_the_strongest_factor_lets_the_step_up_through(action: str, now: float) -> None:
    user, secret = holder("authenticator")
    client = signed_in(user)
    assert secret is not None
    assert reauthenticate(client, user, "mfa", totp_code(secret, now)) == 200

    response = call(client, *ACTIONS[action])

    if action == "add-address":
        assert response.status_code == 200
        assert mailed_to() == [["added@example.com"]]
    else:
        assert response.json()["meta"]["password_changed"] is True
        assert User.objects.get(pk=user.pk).check_password(CHANGED_PASSWORD)


SECOND = "second@example.com"
SPARE = "spare@example.com"
SET_PASSWORD = PASSWORD + "-set"
CALLBACK_PATH = "/api/accounts/google/login/callback/"


@pytest.fixture
def providers(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    serve_provider_profile(monkeypatch)
    with override_settings(SOCIALACCOUNT_PROVIDERS=PROVIDERS):
        yield


def token_of(client: Client) -> str:
    return str(client.get("/api/session/").json()["csrf_token"])


def api(
    client: Client, token: str, method: str, path: str, data: dict[str, Any] | None = None
) -> Any:
    return getattr(client, method)(
        AUTH + path, data or {}, content_type="application/json", HTTP_X_CSRFTOKEN=token
    )


def credentials(user: User) -> dict[str, Any]:
    return {
        "password": User.objects.get(pk=user.pk).password,
        "addresses": list(
            EmailAddress.objects.filter(user=user)
            .order_by("email")
            .values_list("email", "verified", "primary")
        ),
        "authenticators": list(
            Authenticator.objects.filter(user=user).order_by("pk").values_list("pk", "type")
        ),
        "identities": list(
            SocialAccount.objects.filter(user=user).order_by("pk").values_list("provider", "uid")
        ),
    }


def factors(user: User) -> set[str]:
    return {kind for _pk, kind in credentials(user)["authenticators"]}


def addresses(user: User) -> list[str]:
    return [email for email, _verified, _primary in credentials(user)["addresses"]]


def nothing_to_prepare(_client: Client, _now: float) -> Any:
    return None


def activation(client: Client, now: float) -> dict[str, str]:
    secret = client.get(AUTH + "account/authenticators/totp").json()["meta"]["secret"]
    return {"code": totp_code(secret, now)}


def connection_started(client: Client, _now: float) -> str:
    return begin(client, process="connect")


def connection_verified(client: Client, now: float) -> None:
    verified = callback(client, connection_started(client, now))
    assert verified["Location"] == CALLBACK


@dataclass(frozen=True)
class CredentialWrite:
    # A step-up-gated write of the account's own credentials, sent by one session of the account.
    kind: str
    method: str
    path: str
    done: Callable[[User, dict[str, Any], Any], bool]
    data: dict[str, str] | None = None
    prepare: Callable[[Client, float], Any] = nothing_to_prepare
    identity: bool = False
    # Starting an address change or a connection changes no credential by itself.
    changes_state: bool = True

    def send(self, client: Client, token: str, prepared: Any) -> Any:
        if self.method == "form":
            return client.post(self.path, self.data, HTTP_X_CSRFTOKEN=token)
        return getattr(client, self.method)(
            self.path,
            prepared or self.data or {},
            content_type="application/json",
            HTTP_X_CSRFTOKEN=token,
        )


WRITES = {
    "regenerate-recovery-codes": CredentialWrite(
        "authenticator",
        "post",
        AUTH + "account/authenticators/recovery-codes",
        lambda user, before, _: credentials(user)["authenticators"] != before["authenticators"],
    ),
    "remove-authenticator": CredentialWrite(
        "authenticator",
        "delete",
        AUTH + "account/authenticators/totp",
        lambda user, _before, _response: "totp" not in factors(user),
    ),
    "add-authenticator": CredentialWrite(
        "password",
        "post",
        AUTH + "account/authenticators/totp",
        lambda user, _before, _response: "totp" in factors(user),
        prepare=activation,
    ),
    "change-password": CredentialWrite(
        "authenticator",
        "post",
        AUTH + "account/password/change",
        lambda user, _before, _response: User.objects.get(pk=user.pk).check_password(
            CHANGED_PASSWORD
        ),
        {"current_password": PASSWORD, "new_password": CHANGED_PASSWORD},
    ),
    "set-password": CredentialWrite(
        "code",
        "post",
        AUTH + "account/password/change",
        lambda user, _before, _response: User.objects.get(pk=user.pk).check_password(SET_PASSWORD),
        {"new_password": SET_PASSWORD},
    ),
    "add-address": CredentialWrite(
        "password",
        "post",
        AUTH + "account/email",
        lambda _user, _before, response: (
            response.status_code == 200 and ["added@example.com"] in mailed_to()
        ),
        {"email": "added@example.com"},
        changes_state=False,
    ),
    "remove-address": CredentialWrite(
        "password",
        "delete",
        AUTH + "account/email",
        lambda user, _before, _response: SPARE not in addresses(user),
        {"email": SPARE},
    ),
    "remove-identity": CredentialWrite(
        "password",
        "delete",
        AUTH + "account/providers",
        lambda user, _before, _response: credentials(user)["identities"] == [],
        {"provider": "google", "account": "google-identity"},
        identity=True,
    ),
    "connect-redirect": CredentialWrite(
        "password",
        "form",
        AUTH + "auth/provider/redirect",
        lambda _user, _before, response: bool(provider_destination(response)),
        {"provider": "google", "process": "connect", "callback_url": CALLBACK},
        changes_state=False,
    ),
    "connect": CredentialWrite(
        "password",
        "post",
        AUTH + "auth/provider/complete",
        lambda user, _before, _response: (
            credentials(user)["identities"] == [("google", "google-identity")]
        ),
        prepare=connection_verified,
    ),
}


def arranged(write: CredentialWrite) -> tuple[User, Client, Client]:
    # One account with two sessions, each holding a proof by the strongest factor.
    user, _secret = holder(write.kind)
    if STRONGEST[write.kind] == "mfa":
        RecoveryCodes.activate(user)
    for address in (SECOND, SPARE):
        EmailAddress.objects.create(user=user, email=address, verified=True)
    if write.identity:
        SocialAccount.objects.create(user=user, provider="google", uid="google-identity")
    factor = STRONGEST[write.kind]
    extra: dict[str, Any] = {}
    if factor == "mfa":
        totp = Authenticator.objects.get(user=user, type="totp")
        extra = {"type": totp.type, "id": totp.pk}
    sessions = [signed_in(user), signed_in(user)]
    for session in sessions:
        with_proof(session, user, factor, age=0, reauthenticated=True, **extra)
    return user, sessions[0], sessions[1]


def authenticator_removal(other: Client, token: str, _prepared: Any) -> Any:
    return api(other, token, "delete", "account/authenticators/totp")


def authenticator_activation(other: Client, token: str, prepared: Any) -> Any:
    return api(other, token, "post", "account/authenticators/totp", prepared)


def address_removal(other: Client, token: str, _prepared: Any) -> Any:
    return api(other, token, "delete", "account/email", {"email": SECOND})


OTHER_PASSWORD = PASSWORD + "-other"


def password_setting(other: Client, token: str, _prepared: Any) -> Any:
    return api(other, token, "post", "account/password/change", {"new_password": OTHER_PASSWORD})


def password_removal(user: User) -> Callable[[Client, str, Any], None]:
    def remove(_other: Client, _token: str, _prepared: Any) -> None:
        User.objects.filter(pk=user.pk).update(password=make_password(None))

    return remove


def accepted(response: Any) -> bool:
    return bool(response.status_code == 200)


def password_was_set(response: Any) -> bool:
    # A password change signs the session that made it out: 401 with the change noted.
    return response.json()["meta"].get("password_changed") is True


@dataclass(frozen=True)
class CompetingChange:
    # A credential change of the same account made by the other session while the write runs.
    kinds: frozenset[str]
    make: Callable[[User], Callable[[Client, str, Any], Any]]
    applied: Callable[[User], bool]
    prepare: Callable[[Client, float], Any] = nothing_to_prepare
    answered: Callable[[Any], bool] = accepted
    # No route removes a password: that change is written to the account row directly, which
    # signs the sessions out through their password hash instead of revoking them.
    routed: bool = True
    revokes_sessions: bool = False


COMPETING = {
    "address-removed": CompetingChange(
        frozenset({"authenticator", "password", "code"}),
        lambda _user: address_removal,
        lambda user: SECOND not in addresses(user),
    ),
    "last-authenticator-removed": CompetingChange(
        frozenset({"authenticator"}),
        lambda _user: authenticator_removal,
        lambda user: factors(user) == set(),
        revokes_sessions=True,
    ),
    "first-authenticator-added": CompetingChange(
        frozenset({"password", "code"}),
        lambda _user: authenticator_activation,
        lambda user: "totp" in factors(user),
        prepare=activation,
        revokes_sessions=True,
    ),
    "password-set": CompetingChange(
        frozenset({"code"}),
        lambda _user: password_setting,
        lambda user: User.objects.get(pk=user.pk).check_password(OTHER_PASSWORD),
        answered=password_was_set,
        revokes_sessions=True,
    ),
    "password-removed": CompetingChange(
        frozenset({"password"}),
        password_removal,
        lambda user: not User.objects.get(pk=user.pk).has_usable_password(),
        routed=False,
    ),
}
COMPETITIONS = [
    pytest.param(write, change, id=f"{write}-against-{change}")
    for write, case in WRITES.items()
    for change, competing in COMPETING.items()
    if case.kind in competing.kinds
]


def competing(
    change: CompetingChange, user: User, other: Client, now: float
) -> tuple[list[Any], Callable[[], object]]:
    prepared = change.prepare(other, now)
    token = token_of(other)
    made = change.make(user)
    answers: list[Any] = []
    return answers, lambda: answers.append(made(other, token, prepared))


def assert_refused(write: CredentialWrite, response: Any, user: User) -> None:
    # The refusal asks for the factor the account's state after the change demands.
    if write.method == "form":
        assert response.status_code == 302
        assert "error=reauthentication_required" in response["Location"]
    else:
        assert response.status_code == 401
        flows = [flow["id"] for flow in response.json()["data"]["flows"]]
        assert flows == [FLOWS[strongest_now(user)]]


NOTHING_PENDING = CALLBACK + "&error=unknown&error_process=login"


def assert_ended(response: Any) -> None:
    # The change revoked this session while its request ran.
    assert (response.status_code, response.json()["code"]) == (400, "session_ended")


def assert_refused_or_ended(response: Any, user: User) -> None:
    # Refused under the locks; a write that ended the session answers that the session ended.
    if response.status_code == 400:
        assert response.json()["code"] == "session_ended"
        return
    assert response.status_code == 401
    flows = [flow["id"] for flow in response.json()["data"]["flows"]]
    assert flows == [FLOWS[strongest_now(user)]]


def cross_the_window(monkeypatch: pytest.MonkeyPatch) -> None:
    # Every reader of the reauthentication window sees it passed for the session's proofs.
    clock = moment(time.time() + account_settings.REAUTHENTICATION_TIMEOUT + 1)
    for module in (processes, security, reauthentication):
        monkeypatch.setattr(module, "time", clock)


# Where the other session's change commits in the request of the write: after the session and its
# user were read, after the request-start comparison of the session's record, or while the write
# waits for the account's locks, where its proofs also pass the reauthentication window.
POINTS = ["session-read", "request-start", "waiting-for-locks"]


def changed_at(
    point: str,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    user: User,
    change: Callable[[], object],
) -> list[dict[str, Any]]:
    # Commits the change from another session at `point` of the request to `path`, and notes the
    # credentials it left.
    after: list[dict[str, Any]] = []
    started = threading.Event()

    def commit_the_change() -> None:
        started.set()
        elsewhere(change)
        elsewhere(deliver_mail)
        after.append(credentials(user))
        mail.outbox.clear()

    if point == "waiting-for-locks":
        state = processes.account_state
        thread = threading.get_ident()

        def change_before_the_locks(user_id: Any, *, lock: bool = False) -> str | None:
            if lock and not started.is_set() and threading.get_ident() == thread:
                commit_the_change()
                cross_the_window(monkeypatch)
            return state(user_id, lock=lock)

        monkeypatch.setattr(processes, "account_state", change_before_the_locks)
        return after
    compare = processes.end_stale_reauthentication

    def change_at_the_start(request: Any) -> None:
        if request.path != path or started.is_set():
            compare(request)
        elif point == "session-read":
            assert request.user.is_authenticated
            commit_the_change()
            compare(request)
        else:
            compare(request)
            commit_the_change()

    monkeypatch.setattr(processes, "end_stale_reauthentication", change_at_the_start)
    return after


def changed_after_the_check(
    monkeypatch: pytest.MonkeyPatch, path: str, user: User, change: Callable[[], object]
) -> list[dict[str, Any]]:
    return changed_at("request-start", monkeypatch, path, user, change)


Changes = list[tuple[bool, Future[None]]]


def changing_after_the_gate(
    monkeypatch: pytest.MonkeyPatch, change: Callable[[], object]
) -> Changes:
    # Starts the change from another connection once this thread's request has passed its step-up
    # gate, and notes whether the change had committed when the request went on.
    changes: Changes = []
    thread = threading.get_ident()

    def after(gate: Callable[..., bool]) -> Callable[..., bool]:
        def gated(*args: Any, **kwargs: Any) -> bool:
            passed = gate(*args, **kwargs)
            if passed and not changes and threading.get_ident() == thread:
                future = alongside(change)
                changes.append((future.done(), future))
            return passed

        return gated

    monkeypatch.setattr(
        reauthentication,
        "did_recently_reauthenticate",
        after(reauthentication.did_recently_reauthenticate),
    )
    monkeypatch.setattr(step_up, "has_mfa_evidence", after(security.has_mfa_evidence))
    return changes


@pytest.mark.parametrize("point", POINTS)
@pytest.mark.parametrize(("write", "change"), COMPETITIONS)
def test_a_credential_write_is_refused_after_another_sessions_credential_change_committed(
    write: str,
    change: str,
    point: str,
    now: float,
    providers: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case, competitor = WRITES[write], COMPETING[change]
    user, client, other = arranged(case)
    prepared = case.prepare(client, now)
    answers, make_change = competing(competitor, user, other, now)
    token = token_of(client)
    after = changed_at(point, monkeypatch, case.path, user, make_change)
    client.raise_request_exception = not competitor.revokes_sessions

    response = case.send(client, token, prepared)

    if competitor.routed:
        assert competitor.answered(answers[0])
    assert competitor.applied(user)
    if competitor.revokes_sessions:
        assert_ended(response)
    else:
        assert_refused(case, response, user)
    # Ending the proofs changes the session's data under its key; no answer sends the cookie.
    assert session_cookie(response) is None
    assert credentials(user) == after[0]
    assert mailed_to() == []
    signed_out = competitor.revokes_sessions or not competitor.routed
    assert session_user(client) == (None if signed_out else user.pk)
    assert proofs(client) == []


@pytest.mark.parametrize(("write", "change"), COMPETITIONS)
def test_a_credential_change_attempted_while_a_credential_write_runs_waits_and_ends_the_proofs(
    write: str, change: str, now: float, providers: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    case, competitor = WRITES[write], COMPETING[change]
    user, client, other = arranged(case)
    prepared = case.prepare(client, now)
    answers, make_change = competing(competitor, user, other, now)
    token = token_of(client)
    before = credentials(user)
    mail.outbox.clear()
    changes = changing_after_the_gate(monkeypatch, make_change)

    response = case.send(client, token, prepared)
    [(committed, waited)] = changes
    waited.result(timeout=10)

    assert not committed
    assert case.done(user, before, response)
    if not competitor.routed:
        assert competitor.applied(user)
    elif case.changes_state:
        assert_refused_or_ended(answers[0], user)
    else:
        assert competitor.answered(answers[0])
        assert competitor.applied(user)
    if change == "address-removed" and case.changes_state:
        assert SECOND in addresses(user)
    # Only the session whose own write left the account's last state keeps its confirmation.
    last_writer = None if not competitor.routed else client if case.changes_state else other
    for session in (client, other):
        still_signed_in = session_user(session) is not None
        assert bool(proofs(session)) is (still_signed_in and session is last_writer)


def test_a_regeneration_after_another_sessions_regeneration_committed_stores_nothing(
    now: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = WRITES["regenerate-recovery-codes"]
    user, client, other = arranged(case)
    token, other_token = token_of(client), token_of(other)
    regenerated: list[Any] = []
    after = changed_after_the_check(
        monkeypatch,
        case.path,
        user,
        lambda: regenerated.append(
            api(other, other_token, "post", "account/authenticators/recovery-codes")
        ),
    )

    response = case.send(client, token, None)

    assert regenerated[0].status_code == 200
    # The regeneration elsewhere ended this session; its refusal answers that.
    assert (response.status_code, response.json()["code"]) == (400, "session_ended")
    assert credentials(user) == after[0]
    assert session_user(client) is None


def test_a_password_change_reads_the_current_password_under_the_accounts_locks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user, _secret = holder("password")
    client, other = signed_in(user), signed_in(user)
    token, other_token = token_of(client), token_of(other)
    elsewhere_password = PASSWORD + "-elsewhere"
    changed: list[Any] = []
    after = changed_after_the_check(
        monkeypatch,
        AUTH + "account/password/change",
        user,
        lambda: changed.append(
            api(
                other,
                other_token,
                "post",
                "account/password/change",
                {"current_password": PASSWORD, "new_password": elsewhere_password},
            )
        ),
    )

    response = api(
        client,
        token,
        "post",
        "account/password/change",
        {"current_password": PASSWORD, "new_password": CHANGED_PASSWORD},
    )

    assert changed[0].json()["meta"]["password_changed"] is True
    # The change elsewhere ended this session, which the locked check reads first.
    assert (response.status_code, response.json()["code"]) == (400, "session_ended")
    assert User.objects.get(pk=user.pk).check_password(elsewhere_password)
    assert credentials(user) == after[0]
    assert session_user(client) is None


@pytest.mark.parametrize("point", POINTS)
def test_a_password_change_whose_proof_ended_is_refused_when_the_last_authenticator_went(
    point: str, now: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The current password alone would pass the gate the account has after the removal.
    user, client, other = arranged(WRITES["change-password"])
    token, other_token = token_of(client), token_of(other)
    removed: list[Any] = []
    changed_at(
        point,
        monkeypatch,
        AUTH + "account/password/change",
        user,
        lambda: removed.append(authenticator_removal(other, other_token, None)),
    )

    response = api(
        client,
        token,
        "post",
        "account/password/change",
        {"current_password": PASSWORD, "new_password": CHANGED_PASSWORD},
    )

    assert removed[0].status_code == 200
    assert factors(user) == set()
    assert (response.status_code, response.json()["code"]) == (400, "session_ended")
    assert (
        submit(browser(), "auth/login", {"email": user.email, "password": PASSWORD}).status_code
        == 200
    )
    assert (
        submit(
            browser(), "auth/login", {"email": user.email, "password": CHANGED_PASSWORD}
        ).status_code
        == 400
    )


@pytest.mark.parametrize("order", ["processed before", "processed after"])
def test_a_proof_recorded_with_the_key_rotation_serves_the_next_request_of_that_browser(
    order: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The proof is stamped in the transaction that rotates the session key: the browser gets the
    # rotated key once, and an answer for the old key arriving after it changes nothing.
    user, _secret = holder("password")
    client = account_sessions.signed_in(user)
    old = session_key(client)
    stale = carrying_key(old)

    def reauthenticate_by_password() -> Any:
        return submit(client, "auth/reauthenticate", {"password": PASSWORD})

    if order == "processed before":
        # The background read records its activity, so it saves the session it carries.
        monkeypatch.setattr(account_middleware, "time", moment(time.time() + 61))
        late = stale.get("/api/tenants/")
        rotation = reauthenticate_by_password()
        assert late.status_code == 200
    else:
        rotation = reauthenticate_by_password()
        late = stale.get("/api/tenants/")
        assert (late.status_code, late.json()["code"]) == (401, "not_authenticated")
    rotated = session_key(client)
    ninety_days = 90 * 24 * 60 * 60
    key, lifetime = lasting_session_cookie(rotation)
    assert key == rotated
    assert ninety_days - 5 <= lifetime <= ninety_days
    jar = carrying_key(old)

    arrive(jar, rotation, late)
    write = submit(jar, "account/email", {"email": "added@example.com"})

    assert session_key(jar) == rotated != old
    assert write.status_code == 200
    assert session_cookie(write) is None
    assert ["added@example.com"] in mailed_to()


def test_a_proof_ended_as_a_request_starts_changes_the_session_data_and_sends_no_cookie() -> None:
    user, _secret = holder("password")
    client = account_sessions.signed_in(user, reauthenticated=True)
    key = session_key(client)
    assert proofs(client) != []
    EmailAddress.objects.create(user=user, email=SECOND, verified=True)

    response = client.get("/api/session/")

    assert int(response.json()["user"]["id"]) == user.pk
    assert session_cookie(response) is None
    assert session_key(client) == key
    assert proofs(client) == []


def test_a_provider_callback_whose_session_ended_while_it_ran_answers_like_nothing_pending(
    now: float,
    providers: None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    user, client, _other = arranged(WRITES["connect"])
    state = connection_started(client, now)
    newer: list[Client] = []

    def revoked_then_signed_in_again() -> None:
        end_sessions(user.pk, "mfa_reset")
        newer.append(account_sessions.signed_in(user))

    changed_after_the_check(monkeypatch, CALLBACK_PATH, user, revoked_then_signed_in_again)
    monkeypatch.setattr(refusal_throttle, "_seen", OrderedDict())
    events = capture_security_events(caplog, monkeypatch)

    response = callback(client, state)

    refusals = [
        (event["reason"], event["user_id"])
        for event in events()
        if event["event"] == "social_login_refused"
    ]
    assert (response.status_code, response["Location"]) == (302, NOTHING_PENDING)
    assert refusals == [("provider_error", None)]
    assert session_cookie(response) is None
    assert credentials(user)["identities"] == []
    [established] = newer
    jar = carrying_key(session_key(established))
    arrive(jar, response)
    assert session_user(jar) == user.pk
    # The same browser's callback, its session gone, finds nothing pending and answers alike.
    assert callback(client, state)["Location"] == response["Location"]


def test_a_provider_callback_whose_session_save_times_out_answers_busy_and_keeps_its_state(
    now: float, providers: None
) -> None:
    user, client, _other = arranged(WRITES["connect"])
    state = connection_started(client, now)
    client.raise_request_exception = False

    with session_row_held(session_key(client)):
        response = callback(client, state)

    assert response.status_code == 503
    assert response.json()["code"] == "busy"
    assert session_cookie(response) is None
    assert credentials(user)["identities"] == []
    assert session_user(client) == user.pk
    assert state in client.session["socialaccount_states"]


@pytest.mark.parametrize("change", ["first-authenticator-added", "address-removed"])
def test_a_change_attempted_while_the_connect_redirect_saves_its_state_commits_after_it(
    change: str, now: float, providers: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    case, competitor = WRITES["connect-redirect"], COMPETING[change]
    user, client, other = arranged(case)
    answers, make_change = competing(competitor, user, other, now)
    token = token_of(client)
    changes: Changes = []
    stash = statekit.stash_state

    def change_then_stash(request: Any, **kwargs: Any) -> str:
        if not changes:
            future = alongside(make_change)
            changes.append((future.done(), future))
        return str(stash(request, **kwargs))

    monkeypatch.setattr(statekit, "stash_state", change_then_stash)

    response = case.send(client, token, None)
    [(committed, waited)] = changes
    waited.result(timeout=10)

    assert not committed
    state = parse_qs(urlsplit(provider_destination(response)).query)["state"][0]
    assert competitor.answered(answers[0])
    assert competitor.applied(user)
    verified = callback(client, state)
    if competitor.revokes_sessions:
        # The pending connection ended with the session the change ended.
        assert verified["Location"] == NOTHING_PENDING
    else:
        assert verified["Location"] == CALLBACK
        assert_refused(WRITES["connect"], finish(client), user)
    assert credentials(user)["identities"] == []


def test_a_refused_connection_leaves_an_existing_identity_as_it_was(
    now: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = serve_provider_profile(monkeypatch)
    with override_settings(SOCIALACCOUNT_PROVIDERS=PROVIDERS):
        user, client, _other = arranged(WRITES["remove-identity"])
        SocialAccount.objects.filter(user=user).update(extra_data={"email": "old@gmail.com"})
        state = connection_started(client, now)

        def password_removed() -> None:
            elsewhere(lambda: User.objects.filter(pk=user.pk).update(password=make_password(None)))

        profile["_before_profile"] = password_removed

        response = complete(client, state)

    # Removing the password signed the session out, so no login is stored for it.
    assert response.status_code == 409
    assert list(SocialAccount.objects.values_list("user_id", "uid", "extra_data")) == [
        (user.pk, "google-identity", {"email": "old@gmail.com"})
    ]


def activation_by(_user: User, other: Client, now: float) -> Callable[[], Any]:
    prepared, token = activation(other, now), token_of(other)
    return lambda: authenticator_activation(other, token, prepared)


def removal_by(_user: User, other: Client, _now: float) -> Callable[[], Any]:
    token = token_of(other)
    return lambda: authenticator_removal(other, token, None)


def password_change_by(_user: User, other: Client, _now: float) -> Callable[[], Any]:
    token = token_of(other)
    data = {"current_password": PASSWORD, "new_password": OTHER_PASSWORD}
    return lambda: api(other, token, "post", "account/password/change", data)


def operator_reset(user: User, _other: Client, _now: float) -> Callable[[], Any]:
    return lambda: run("reset_mfa", [user.email], email=user.email, reason="lost_device")


@dataclass(frozen=True)
class CallbackCompetitor:
    # A change of the connecting account by its other session or by the operator; each ends the
    # connecting session.
    kind: str
    prepare: Callable[[User, Client, float], Callable[[], Any]]
    applied: Callable[[User], bool]


AGAINST_THE_CALLBACK = {
    "authenticator-activation": CallbackCompetitor(
        "password", activation_by, lambda user: "totp" in factors(user)
    ),
    "authenticator-removal": CallbackCompetitor(
        "authenticator", removal_by, lambda user: "totp" not in factors(user)
    ),
    "password-change": CallbackCompetitor(
        "password",
        password_change_by,
        lambda user: User.objects.get(pk=user.pk).check_password(OTHER_PASSWORD),
    ),
    "operator-reset": CallbackCompetitor(
        "authenticator", operator_reset, lambda user: factors(user) == set()
    ),
}


def connecting(competitor: CallbackCompetitor, now: float) -> tuple[User, Client, str, Any]:
    user, client, other = arranged(replace(WRITES["connect"], kind=competitor.kind))
    state = connection_started(client, now)
    return user, client, state, competitor.prepare(user, other, now)


@pytest.mark.parametrize("change", AGAINST_THE_CALLBACK)
def test_a_change_during_the_provider_exchange_waits_for_nothing_and_ends_the_connection(
    change: str, now: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    competitor = AGAINST_THE_CALLBACK[change]
    profile = serve_provider_profile(monkeypatch)
    with override_settings(SOCIALACCOUNT_PROVIDERS=PROVIDERS):
        user, client, state, make_change = connecting(competitor, now)
        changes: Changes = []

        def change_during_the_exchange() -> None:
            if not changes:
                future = alongside(make_change)
                changes.append((future.done(), future))

        profile["_before_profile"] = change_during_the_exchange

        response = complete(client, state)
    [(finished, future)] = changes
    future.result(timeout=10)

    # The callback held no lock while it waited for the provider.
    assert finished
    assert competitor.applied(user)
    # The completion finds no login stored for the session that ended.
    assert response.status_code == 409
    assert credentials(user)["identities"] == []
    assert session_user(client) is None


@pytest.mark.parametrize("change", AGAINST_THE_CALLBACK)
def test_a_change_attempted_while_the_completion_connects_waits_for_the_connection(
    change: str, now: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    competitor = AGAINST_THE_CALLBACK[change]
    serve_provider_profile(monkeypatch)
    with override_settings(SOCIALACCOUNT_PROVIDERS=PROVIDERS):
        user, client, state, make_change = connecting(competitor, now)
        answers: list[Any] = []
        changes = changing_after_the_gate(monkeypatch, lambda: answers.append(make_change()))

        response = complete(client, state)
    [(committed, waited)] = changes
    waited.result(timeout=10)

    assert not committed
    assert response.status_code == 200
    assert credentials(user)["identities"] == [("google", "google-identity")]
    if change == "operator-reset":
        assert competitor.applied(user)
        assert session_user(client) is None
    else:
        # The connection changed the state the other session's record names.
        assert_refused_or_ended(answers[0], user)
        assert not competitor.applied(user)


@pytest.mark.parametrize("process", ["login", "connect"])
def test_no_provider_request_runs_inside_a_database_transaction(
    process: str, now: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    serve_provider_profile(monkeypatch)
    served = requests.Session.request
    in_transaction: list[bool] = []

    def observed(self: requests.Session, method: str, url: str, *args: Any, **kwargs: Any) -> Any:
        in_transaction.append(transaction.get_connection().in_atomic_block)
        return served(self, method, url, *args, **kwargs)

    monkeypatch.setattr(requests.Session, "request", observed)
    with override_settings(SOCIALACCOUNT_PROVIDERS=PROVIDERS):
        if process == "login":
            arranged(WRITES["remove-identity"])
            client = browser()
            state = begin(client)
        else:
            _user, client, _other = arranged(WRITES["connect"])
            state = connection_started(client, now)

        response = callback(client, state)

    assert response["Location"] == CALLBACK
    # The token request and the profile request.
    assert in_transaction == [False, False]


# allauth sends an activation's signal from the commit already; a removal sends it in the
# transaction of the write.
FAILING_AFTER = {
    "add-authenticator": ("activate_totp", "mfa_added"),
    "remove-authenticator": ("deactivate_totp", "mfa_removed"),
}


@pytest.mark.parametrize("write", FAILING_AFTER)
def test_a_factor_write_that_rolls_back_leaves_no_security_line_and_no_mail(
    write: str, now: float, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    case = WRITES[write]
    user, client, other = arranged(case)
    prepared = case.prepare(client, now)
    token = token_of(client)
    before = credentials(user)
    events = capture_security_events(caplog, monkeypatch)
    name, event = FAILING_AFTER[write]
    flow = getattr(totp_flows, name)

    def written_then_failed(*args: Any) -> Any:
        flow(*args)
        raise DatabaseError("The transaction failed after the write.")

    monkeypatch.setattr(totp_flows, name, written_then_failed)
    client.raise_request_exception = False

    response = case.send(client, token, prepared)

    assert response.status_code >= 500
    assert credentials(user) == before
    assert {event, "sessions_revoked"} & {line["event"] for line in events()} == set()
    assert list(OutboundMail.objects.values_list("kind", flat=True)) == []
    assert mailed_to() == []
    assert session_user(other) == user.pk


def test_an_address_change_whose_session_cannot_be_saved_sends_no_code(
    now: float, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    case = WRITES["add-address"]
    user, client, _other = arranged(case)
    token = token_of(client)
    events = capture_security_events(caplog, monkeypatch)
    mail.outbox.clear()
    client.raise_request_exception = False

    with session_row_held(session_key(client)):
        response = case.send(client, token, None)

    assert list(OutboundMail.objects.values_list("kind", flat=True)) == []
    assert mailed_to() == []
    assert "email_change_requested" not in {line["event"] for line in events()}
    assert (response.status_code, response.json()["code"]) == (503, "busy")
    assert session_user(client) == user.pk


def test_a_reauthentication_code_whose_session_cannot_be_saved_is_not_queued() -> None:
    user, _secret = holder("code")
    client = signed_in(user)
    token = token_of(client)
    client.raise_request_exception = False

    with session_row_held(session_key(client)):
        response = api(
            client, token, "post", "auth/reauthenticate/code/request", {"email": user.email}
        )

    assert list(OutboundMail.objects.values_list("kind", flat=True)) == []
    assert mailed_to() == []
    assert (response.status_code, response.json()["code"]) == (503, "busy")
    assert session_user(client) == user.pk


@pytest.mark.parametrize("write", ["add-authenticator", "regenerate-recovery-codes"])
def test_a_factor_write_whose_notice_cannot_be_stored_stores_nothing(
    write: str, now: float, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    case = WRITES[write]
    user, client, other = arranged(case)
    prepared = case.prepare(client, now)
    token = token_of(client)
    token_of(other)
    before = credentials(user)
    events = capture_security_events(caplog, monkeypatch)
    written = len(events())

    def refused(*_args: Any, **_kwargs: Any) -> None:
        raise DatabaseError("The notice could not be stored.")

    monkeypatch.setattr(adapters, "enqueue_mail", refused)
    client.raise_request_exception = False

    response = case.send(client, token, prepared)

    assert response.status_code == 500
    assert credentials(user) == before
    assert session_user(other) == user.pk
    assert events()[written:] == []
    assert list(OutboundMail.objects.values_list("kind", flat=True)) == []
    assert mailed_to() == []


@pytest.mark.parametrize("write", ["add-authenticator", "regenerate-recovery-codes"])
def test_a_factor_write_that_cannot_end_the_other_sessions_stores_nothing(
    write: str, now: float
) -> None:
    case = WRITES[write]
    user, client, other = arranged(case)
    prepared = case.prepare(client, now)
    token = token_of(client)
    # Its first request records the other session's device, through which revocation finds it.
    token_of(other)
    before = credentials(user)
    client.raise_request_exception = False

    with session_row_held(session_key(other)):
        response = case.send(client, token, prepared)

    assert credentials(user) == before
    assert (response.status_code, response.json()["code"]) == (503, "busy")
    assert session_user(other) == user.pk


def test_a_same_browser_request_does_not_wait_for_the_provider_exchange(
    now: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = serve_provider_profile(monkeypatch)
    with override_settings(SOCIALACCOUNT_PROVIDERS=PROVIDERS):
        _user, client, _other = arranged(WRITES["connect"])
        state = connection_started(client, now)
        key = session_key(client)
        started: Changes = []

        def same_browser_during_the_exchange() -> None:
            if not started:
                future = alongside(lambda: carrying_key(key).get("/api/session/"))
                started.append((future.done(), future))

        profile["_before_profile"] = same_browser_during_the_exchange

        response = callback(client, state)
    [(finished, future)] = started
    future.result(timeout=10)

    assert finished
    assert response["Location"] == CALLBACK


def test_a_connect_redirect_whose_session_ends_after_its_commit_sends_no_session_cookie(
    providers: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = WRITES["connect-redirect"]
    user, client, _other = arranged(case)
    token = token_of(client)
    post = social_views.RedirectToProviderView.post

    def committed_then_ended(self: Any, request: Any, *args: Any, **kwargs: Any) -> Any:
        response = post(self, request, *args, **kwargs)
        elsewhere(lambda: end_sessions(user.pk, "mfa_reset"))
        return response

    monkeypatch.setattr(social_views.RedirectToProviderView, "post", committed_then_ended)

    response = case.send(client, token, None)

    assert provider_destination(response)
    assert session_cookie(response) is None


def test_a_provider_exchange_that_fails_after_its_session_ended_sends_no_session_cookie(
    now: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = serve_provider_profile(monkeypatch)
    with override_settings(SOCIALACCOUNT_PROVIDERS=PROVIDERS):
        user, client, _other = arranged(WRITES["connect"])
        state = connection_started(client, now)

        def ended_then_failed() -> None:
            elsewhere(lambda: end_sessions(user.pk, "mfa_reset"))
            raise requests.ConnectionError("The provider is unreachable.")

        profile["_before_profile"] = ended_then_failed

        response = callback(client, state)

    assert response["Location"] == CALLBACK + "&error=unknown&error_process=connect"
    assert session_cookie(response) is None
    assert session_user(client) is None


def test_an_expired_provider_state_answers_its_error_without_a_cookie_after_its_session_ended(
    now: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    serve_provider_profile(monkeypatch)
    with override_settings(SOCIALACCOUNT_PROVIDERS=PROVIDERS):
        user, client, _other = arranged(WRITES["connect"])
        state = connection_started(client, now)
        dispatch = social_views.ProviderCallbackView.dispatch

        def answered_then_ended(self: Any, request: Any, *args: Any, **kwargs: Any) -> Any:
            response = dispatch(self, request, *args, **kwargs)
            elsewhere(lambda: end_sessions(user.pk, "mfa_reset"))
            return response

        monkeypatch.setattr(social_views.ProviderCallbackView, "dispatch", answered_then_ended)
        later = time.time() + statekit.STATE_TTL + 1
        monkeypatch.setattr(statekit, "time", SimpleNamespace(time=lambda: later))

        response = callback(client, state)

    assert (response.status_code, response.get("Location"), session_cookie(response)) == (
        302,
        CALLBACK + "&error=unknown&error_process=login",
        None,
    )
    assert session_user(client) is None


class SignalAfterSignIn:
    # Signs a new session of the account in after the write committed, before the library sends
    # its signal from that commit.
    def __init__(self, signal: Any, user: User, signed: list[Client]) -> None:
        self.signal, self.user, self.signed = signal, user, signed

    def send(self, **kwargs: Any) -> Any:
        if not self.signed:
            elsewhere(self.sign_in)
        return self.signal.send(**kwargs)

    def sign_in(self) -> None:
        client = signed_in(self.user)
        token_of(client)
        self.signed.append(client)


@pytest.mark.parametrize("write", ["add-authenticator", "regenerate-recovery-codes"])
def test_a_factor_write_ends_the_other_sessions_once_in_its_transaction(
    write: str, now: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = WRITES[write]
    user, client, other = arranged(case)
    prepared = case.prepare(client, now)
    token = token_of(client)
    token_of(other)
    signed: list[Client] = []
    sent = SimpleNamespace(
        **{
            name: SignalAfterSignIn(getattr(mfa_signals, name), user, signed)
            for name in ("authenticator_added", "authenticator_removed", "authenticator_reset")
        }
    )
    for flows in (totp_flows, recovery_code_flows):
        monkeypatch.setattr(flows, "signals", sent)

    response = case.send(client, token, prepared)

    assert response.status_code == 200
    assert session_user(other) is None
    [later] = signed
    assert session_user(later) == user.pk


def test_a_refused_address_change_does_not_save_its_session() -> None:
    case = WRITES["add-address"]
    _user, client, _other = arranged(case)
    token = token_of(client)

    with CaptureQueriesContext(connection) as queries:
        response = api(client, token, "post", "account/email", {"email": "not-an-address"})

    assert response.status_code == 400
    assert not [
        query
        for query in queries.captured_queries
        if query["sql"].startswith('UPDATE "django_session"')
    ]
