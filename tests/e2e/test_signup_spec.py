from pathlib import Path

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth.models import User
from django.contrib.sessions.models import Session

from pyracast.accounts.models import Membership, WorkspaceRole
from tests.support.browser import PASSWORD, Gateway, run_spec

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])


def test_a_new_company_signs_up_verifies_its_email_and_signs_out(
    web_gateway: Gateway, auth_mailbox: Path
) -> None:
    email = "signup@example.com"

    run_spec(
        "signup.spec.ts",
        web_gateway,
        BROWSER_SIGNUP_EMAIL=email,
        BROWSER_SIGNUP_PASSWORD=PASSWORD,
        BROWSER_AUTH_MAILBOX=str(auth_mailbox),
    )

    user = User.objects.get(email=email)
    assert user.check_password(PASSWORD)
    assert EmailAddress.objects.filter(user=user, email=email, primary=True, verified=True).exists()
    membership = Membership.objects.select_related("tenant").get(user=user)
    assert (membership.tenant.name, membership.role) == (
        "Browser signup company",
        WorkspaceRole.ADMIN,
    )
    assert not [
        session
        for session in Session.objects.all()
        if session.get_decoded().get("_auth_user_id") == str(user.pk)
    ]
