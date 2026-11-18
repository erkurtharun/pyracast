import time
from collections.abc import Iterator
from types import SimpleNamespace

import pytest
from allauth.account.models import EmailAddress
from allauth.mfa.totp.internal import auth as totp_auth
from django.conf import settings
from django.contrib.auth.models import User
from django.core import mail
from django.test import Client, override_settings

from tests.support.auth_lifecycle import AUTH, PASSWORD, mailed_code, totp_code
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
    with_proof(client, user, factor, age=0, reauthenticated=True)
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
    with_proof(client, user, "password", age=0, reauthenticated=True)
    token = client.get("/api/session/").json()["csrf_token"]

    response = client.post(
        AUTH + "auth/provider/redirect",
        {
            "provider": "google",
            "process": "connect",
            "callback_url": settings.PYRACAST_SOCIAL_CALLBACK_URL,
        },
        HTTP_X_CSRFTOKEN=token,
    )

    assert response.status_code == 302
    assert "error=reauthentication_required" in response["Location"]


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
