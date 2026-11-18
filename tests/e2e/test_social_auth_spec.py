import pytest
from allauth.socialaccount.models import SocialAccount

from tests.support.browser import run_spec, seed_accounts

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])


def test_sign_in_and_connect_reach_each_federated_provider_under_form_csp(
    web_origin: str,
) -> None:
    accounts = seed_accounts()

    run_spec(
        "social-auth.spec.ts",
        web_origin,
        BROWSER_EMAIL=accounts.system.member.email,
        BROWSER_PASSWORD=accounts.password,
    )

    assert not SocialAccount.objects.exists()
