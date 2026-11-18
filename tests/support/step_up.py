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
from django.test import Client, RequestFactory

from tests.support.auth_lifecycle import PASSWORD

# Each kind of account names the factor a step-up asks of it.
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


def with_proof(
    client: Client, user: User, method: str, *, age: float, reauthenticated: bool, **extra: Any
) -> None:
    # The flag is set after recording, so the signal does not rotate the test client's session key.
    request = RequestFactory().get("/")
    request.user = user
    request.session = client.session
    record_authentication(request, user, method=method, **extra)
    record = get_authentication_records(request, user)[-1]
    record["at"] = time.time() - age
    if reauthenticated:
        record["reauthenticated"] = True
    request.session.save()


def proofs(client: Client) -> list[dict[str, Any]]:
    records = client.session.get(AUTHENTICATION_METHODS_SESSION_KEY)
    return [record for record in records or [] if record.get("reauthenticated")]
