import re
from typing import Any

from allauth.account.models import EmailAddress
from django.contrib.auth.models import User
from django.core import mail
from django.test import Client

from pyracast.accounts.models import Membership, Tenant, WorkspaceRole
from tests.support.outbox import deliver_mail

ACCEPT = "/api/invitations/accept/"
PREVIEW = "/api/invitations/preview/"
UNAVAILABLE = {
    "code": "invitation_unavailable",
    "message": "This invitation is no longer valid. Ask the workspace administrator for a new one.",
}
ALREADY_ACCEPTED = {
    "code": "invitation_unavailable",
    "message": "This invitation was already accepted. Reload the list.",
}


def invitation_token() -> str:
    deliver_mail()
    match = re.search(r"token=([A-Za-z0-9_-]+)", str(mail.outbox[-1].body))
    assert match is not None
    return match.group(1)


def administered_workspace(name: str = "Solar workspace") -> tuple[Tenant, User]:
    tenant = Tenant.objects.create(name=name)
    administrator = User.objects.create_user(username="workspace-admin", email="admin@example.com")
    Membership.objects.create(tenant=tenant, user=administrator, role=WorkspaceRole.ADMIN)
    return tenant, administrator


def verified_account(username: str, email: str) -> User:
    user = User.objects.create_user(username=username, email=email)
    EmailAddress.objects.create(user=user, email=email, verified=True, primary=True)
    return user


def signed_in(user: User, **defaults: Any) -> Client:
    client = Client(**defaults)
    client.force_login(user)
    return client


def post(client: Client, path: str, data: object) -> Any:
    return client.post(path, data, content_type="application/json")
