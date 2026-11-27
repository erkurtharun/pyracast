import pytest
from allauth.mfa.models import Authenticator

from tests.support.browser import Gateway, live_settings, run_spec, seed_accounts

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])


def test_a_customer_enrolls_uses_and_removes_a_passkey(web_gateway: Gateway) -> None:
    accounts = seed_accounts()
    user = accounts.system.member
    # Chromium and fido2 accept localhost as a WebAuthn RP on the loopback HTTP gateway.
    origin = f"http://localhost:{web_gateway.server_port}"
    with live_settings(origin):
        run_spec(
            "passkeys.spec.ts",
            web_gateway,
            origin=origin,
            BROWSER_PASSKEY_EMAIL=user.email,
            BROWSER_PASSKEY_PASSWORD=accounts.password,
        )
    assert not Authenticator.objects.filter(user=user).exists()
