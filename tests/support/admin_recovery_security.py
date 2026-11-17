from typing import Any

from allauth.account.internal.flows.login import record_authentication
from allauth.mfa.totp.internal.auth import TOTP, generate_totp_secret
from django.contrib.auth.models import User
from django.test import Client, RequestFactory

from tests.support.admin_mfa_bootstrap import administrator

AUTH = "/api/auth/browser/v1/"


RECOVERY = AUTH + "account/authenticators/recovery-codes"


def submit(client: Client, path: str, data: dict[str, Any]) -> Any:
    token = client.get("/api/session/").json()["csrf_token"]
    return client.post(path, data, content_type="application/json", HTTP_X_CSRFTOKEN=token)


def authenticated_account(*, privileged: bool = True) -> tuple[Client, User, str]:
    user = administrator()
    if not privileged:
        user.is_staff = user.is_superuser = False
        user.save(update_fields=["is_staff", "is_superuser"])
    secret = generate_totp_secret()
    authenticator = TOTP.activate(user, secret).instance
    client = Client(enforce_csrf_checks=True)
    client.force_login(user)
    request = RequestFactory().get("/")
    request.user = user
    request.session = client.session
    record_authentication(request, user, method="mfa", type="totp", id=authenticator.pk)
    request.session.save()
    return client, user, secret
