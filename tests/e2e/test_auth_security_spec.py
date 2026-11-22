from pathlib import Path
from time import time

import pytest
from allauth.account.models import EmailAddress
from allauth.mfa.models import Authenticator
from django.contrib.auth.models import User

from tests.support.account_sessions import totp_clock
from tests.support.browser import PASSWORD, Gateway, run_spec

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])


def test_a_verified_company_account_is_protected_by_a_second_factor_and_recovery_codes(
    web_gateway: Gateway, auth_mailbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    email = "security@example.com"
    now = int(time())
    totp_clock(monkeypatch, now)

    run_spec(
        "auth-security.spec.ts",
        web_gateway,
        BROWSER_SECURITY_EMAIL=email,
        BROWSER_SECURITY_PASSWORD=PASSWORD,
        BROWSER_AUTH_MAILBOX=str(auth_mailbox),
        BROWSER_TOTP_NOW=str(now),
    )

    changed_email = "security+changed@example.com"
    user = User.objects.get(email=changed_email)
    assert not User.objects.filter(email=email).exists()
    assert list(
        EmailAddress.objects.filter(user=user).values_list("email", "verified", "primary")
    ) == [(changed_email, True, True)]
    assert user.check_password(PASSWORD + "-reset")
    assert {authenticator.type for authenticator in Authenticator.objects.filter(user=user)} == {
        Authenticator.Type.TOTP,
        Authenticator.Type.RECOVERY_CODES,
    }
