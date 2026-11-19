import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import Future
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
from allauth.account.internal.flows import reauthentication
from allauth.account.internal.flows.login import AUTHENTICATION_METHODS_SESSION_KEY
from allauth.account.models import EmailAddress
from allauth.mfa.models import Authenticator
from allauth.mfa.recovery_codes.internal.auth import RecoveryCodes
from allauth.mfa.totp.internal import auth as totp_auth
from allauth.mfa.totp.internal.auth import TOTP, generate_totp_secret
from allauth.socialaccount.models import SocialAccount
from django.conf import settings
from django.contrib.auth.models import User
from django.core import mail
from django.test import Client, override_settings

from pyracast.accounts import processes, security, step_up
from tests.support.account_sessions import alongside, elsewhere, session_user
from tests.support.auth_lifecycle import AUTH, PASSWORD, mailed_code, totp_code
from tests.support.social_accounts import (
    CALLBACK,
    PROVIDERS,
    begin,
    complete,
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
        assert mail.outbox == []


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
    assert mail.outbox == []


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
    assert mail.outbox == []


def test_a_password_proof_ends_when_the_account_gains_an_authenticator(now: float) -> None:
    user, _secret = holder("password")
    client = signed_in(user)
    assert reauthenticate(client, user, "password", "") == 200
    secret = client.get(AUTH + "account/authenticators/totp").json()["meta"]["secret"]
    activation = {"code": totp_code(secret, now)}
    assert call(client, "post", "account/authenticators/totp", activation).status_code == 200
    mail.outbox.clear()

    response = call(client, *ACTIONS["add-address"])

    assert response.status_code == 401
    assert [flow["id"] for flow in response.json()["data"]["flows"]] == [FLOWS["mfa"]]
    assert proofs(client) == []
    assert list(EmailAddress.objects.values_list("email", flat=True)) == [user.email]
    assert mail.outbox == []


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
            assert [message.to for message in mail.outbox] == [["added@example.com"]]
        else:
            assert User.objects.get(pk=user.pk).check_password(CHANGED_PASSWORD)
        return
    assert stored(user) == before
    assert mail.outbox == []
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
    assert mail.outbox == []
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
        assert [message.to for message in mail.outbox] == [["added@example.com"]]
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
    with override_settings(
        SOCIALACCOUNT_PROVIDERS=PROVIDERS,
        HEADLESS_FRONTEND_URLS=settings.HEADLESS_FRONTEND_URLS
        | {"socialaccount_login_error": CALLBACK},
    ):
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
        if self.method == "callback":
            return complete(client, prepared)
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
            response.status_code == 200
            and ["added@example.com"] in [message.to for message in mail.outbox]
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
        "callback",
        CALLBACK_PATH,
        lambda user, _before, _response: (
            credentials(user)["identities"] == [("google", "google-identity")]
        ),
        prepare=connection_started,
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


def assert_refused(write: CredentialWrite, response: Any) -> None:
    if write.method == "callback":
        assert response.status_code == 302
        assert "error=permission_denied" in response["Location"]
    elif write.method == "form":
        assert response.status_code == 302
        assert "error=reauthentication_required" in response["Location"]
    else:
        assert response.status_code == 401
        flows = [flow["id"] for flow in response.json()["data"]["flows"]]
        assert flows == [FLOWS[STRONGEST[write.kind]]]


def assert_refused_or_ended(response: Any, user: User) -> None:
    # Refused under the locks; a write that ended the session answers that the session ended.
    if response.status_code == 400:
        assert response.json()["code"] == "session_ended"
        return
    assert response.status_code == 401
    flows = [flow["id"] for flow in response.json()["data"]["flows"]]
    assert flows == [FLOWS[strongest_now(user)]]


def changed_after_the_check(
    monkeypatch: pytest.MonkeyPatch, path: str, user: User, change: Callable[[], object]
) -> list[dict[str, Any]]:
    # Commits the change from another session once the request to `path` has compared its proof
    # unlocked, as every request does when it starts, and notes the credentials it left.
    after: list[dict[str, Any]] = []
    started = threading.Event()
    checked = processes.end_stale_reauthentication

    def change_after_the_check(request: Any) -> None:
        checked(request)
        if request.path == path and not started.is_set():
            started.set()
            elsewhere(change)
            after.append(credentials(user))
            mail.outbox.clear()

    monkeypatch.setattr(processes, "end_stale_reauthentication", change_after_the_check)
    return after


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


@pytest.mark.parametrize("write", WRITES)
def test_a_credential_write_is_refused_after_another_sessions_credential_change_committed(
    write: str, now: float, providers: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = WRITES[write]
    user, client, other = arranged(case)
    prepared = case.prepare(client, now)
    token, other_token = token_of(client), token_of(other)
    removed: list[Any] = []
    after = changed_after_the_check(
        monkeypatch,
        case.path,
        user,
        lambda: removed.append(
            api(other, other_token, "delete", "account/email", {"email": SECOND})
        ),
    )

    response = case.send(client, token, prepared)

    assert removed[0].status_code == 200
    assert_refused(case, response)
    assert credentials(user) == after[0]
    assert mail.outbox == []
    assert session_user(client) == user.pk
    assert proofs(client) == []


@pytest.mark.parametrize("write", WRITES)
def test_a_credential_change_attempted_while_a_credential_write_runs_waits_and_ends_the_proofs(
    write: str, now: float, providers: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = WRITES[write]
    user, client, other = arranged(case)
    prepared = case.prepare(client, now)
    token, other_token = token_of(client), token_of(other)
    before = credentials(user)
    mail.outbox.clear()
    removed: list[Any] = []
    changes = changing_after_the_gate(
        monkeypatch,
        lambda: removed.append(
            api(other, other_token, "delete", "account/email", {"email": SECOND})
        ),
    )

    response = case.send(client, token, prepared)
    [(committed, change)] = changes
    change.result(timeout=10)

    assert not committed
    assert case.done(user, before, response)
    if case.changes_state:
        assert_refused_or_ended(removed[0], user)
        assert SECOND in addresses(user)
    else:
        assert removed[0].status_code == 200
        assert SECOND not in addresses(user)
    for session in (client, other):
        session_user(session)
        assert proofs(session) == []


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
    assert response.status_code == 400
    assert [error["param"] for error in response.json()["errors"]] == ["current_password"]
    assert User.objects.get(pk=user.pk).check_password(elsewhere_password)
    assert credentials(user) == after[0]
    assert session_user(client) is None
