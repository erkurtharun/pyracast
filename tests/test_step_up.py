import time
from collections.abc import Iterator
from types import SimpleNamespace

import pytest
from allauth.account.models import EmailAddress
from allauth.mfa.totp.internal import auth as totp_auth
from django.contrib.auth.models import User
from django.core import mail
from django.test import Client, override_settings

from tests.support.auth_lifecycle import PASSWORD, mailed_code, totp_code
from tests.support.step_up import FLOWS, STRONGEST, call, holder, proofs, signed_in

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


ACTIONS = {
    "add-address": ("post", "account/email", {"email": "added@example.com"}),
    "set-password": ("post", "account/password/change", {"new_password": PASSWORD + "-set"}),
    "recovery-codes": ("get", "account/authenticators/recovery-codes", None),
    "remove-authenticator": ("delete", "account/authenticators/totp", None),
}
ACCOUNTS_OF = {
    "add-address": set(STRONGEST),
    "set-password": {"code", "authenticator-without-password"},
    "recovery-codes": {"authenticator", "authenticator-without-password"},
    "remove-authenticator": {"authenticator", "authenticator-without-password"},
}
STEP_UPS = [(action, kind) for action, kinds in ACCOUNTS_OF.items() for kind in sorted(kinds)]


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


def test_a_proof_by_the_strongest_factor_lets_the_step_up_through(now: float) -> None:
    user, secret = holder("authenticator")
    client = signed_in(user)
    assert secret is not None
    assert reauthenticate(client, user, "mfa", totp_code(secret, now)) == 200

    response = call(client, "post", "account/email", {"email": "added@example.com"})

    assert response.status_code == 200
    assert [message.to for message in mail.outbox] == [["added@example.com"]]
