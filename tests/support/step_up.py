import time
from typing import Any

from allauth.account.internal.flows.login import (
    AUTHENTICATION_METHODS_SESSION_KEY,
    get_authentication_records,
    record_authentication,
)
from allauth.account.models import EmailAddress
from allauth.mfa.totp.internal.auth import TOTP, generate_totp_secret
from django.contrib.auth.models import User
from django.db import transaction
from django.http import HttpRequest
from django.test import Client, RequestFactory

from pyracast.accounts.processes import AUTHORITY, account_state, record_authority
from tests.support.auth_lifecycle import PASSWORD

STRONGEST = {
    "authenticator": "mfa",
    "password": "password",
    "code": "code",
    "authenticator-without-password": "mfa",
}
FLOWS = {
    "mfa": "mfa_reauthenticate",
    "password": "reauthenticate",
    "code": "reauthenticate_by_code",
}


def holder(kind: str) -> tuple[User, str | None]:
    user = User.objects.create_user(
        username="holder", email="holder@example.com", password=PASSWORD
    )
    EmailAddress.objects.create(user=user, email=user.email, primary=True, verified=True)
    if kind in {"code", "authenticator-without-password"}:
        user.set_unusable_password()
        user.save(update_fields=["password"])
    secret = None
    if STRONGEST[kind] == "mfa":
        secret = generate_totp_secret()
        TOTP.activate(user, secret)
    return user, secret


def signed_in(user: User) -> Client:
    client = Client(enforce_csrf_checks=True)
    client.force_login(user)
    return client


def call(client: Client, method: str, path: str, data: dict[str, Any] | None = None) -> Any:
    token = client.get("/api/session/").json()["csrf_token"]
    return getattr(client, method)(
        "/api/auth/browser/v1/" + path,
        data or {},
        content_type="application/json",
        HTTP_X_CSRFTOKEN=token,
    )


def stamped(request: HttpRequest, user: User) -> None:
    # The product records the account's credential state as it records a reauthentication.
    with transaction.atomic():
        record_authority(request, user)


def with_proof(
    client: Client,
    user: User,
    method: str,
    *,
    age: float,
    reauthenticated: bool,
    retained: bool = False,
    **extra: Any,
) -> None:
    # The flag is set after recording, so the signal does not rotate the test client's session key.
    # A retained proof was recorded in a session from before sessions carried an authority record.
    request = RequestFactory().get("/")
    request.user = user
    request.session = client.session
    record_authentication(request, user, method=method, **extra)
    record = get_authentication_records(request, user)[-1]
    record["at"] = time.time() - age
    if reauthenticated:
        record["reauthenticated"] = True
        if retained:
            request.session.pop(AUTHORITY, None)
        else:
            stamped(request, user)
    request.session.save()


def proofs(client: Client) -> list[dict[str, Any]]:
    records = client.session.get(AUTHENTICATION_METHODS_SESSION_KEY)
    return [record for record in records or [] if record.get("reauthenticated")]


def forget_authority(client: Client) -> None:
    # A session signed in before sessions carried an authority record.
    session = client.session
    session.pop(AUTHORITY, None)
    session.save()


def holds_current_authority(client: Client, user_id: Any) -> bool:
    # The session's authority record names the account's credential state as it is now.
    return bool(client.session.get(AUTHORITY) == [user_id, account_state(user_id)])
