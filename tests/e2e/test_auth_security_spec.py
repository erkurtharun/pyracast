from pathlib import Path

import pytest
from allauth.mfa.models import Authenticator
from django.contrib.auth.models import User

from tests.support.browser import PASSWORD, Gateway, run_spec

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])


def test_a_verified_company_account_is_protected_by_a_second_factor_and_recovery_codes(
    web_gateway: Gateway, auth_mailbox: Path
) -> None:
    email = "security@example.com"

    run_spec(
        "auth-security.spec.ts",
        web_gateway,
        BROWSER_SECURITY_EMAIL=email,
        BROWSER_SECURITY_PASSWORD=PASSWORD,
        BROWSER_AUTH_MAILBOX=str(auth_mailbox),
    )

    user = User.objects.get(email=email)
    assert user.check_password(PASSWORD + "-reset")
    assert {authenticator.type for authenticator in Authenticator.objects.filter(user=user)} == {
        Authenticator.Type.TOTP,
        Authenticator.Type.RECOVERY_CODES,
    }
