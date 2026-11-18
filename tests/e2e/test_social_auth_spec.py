import pytest
from allauth.socialaccount.models import SocialAccount

from tests.support.browser import Gateway, run_spec, seed_accounts, seed_forecast_days

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])


def test_sign_in_and_connect_reach_each_federated_provider_under_form_csp(
    web_gateway: Gateway,
) -> None:
    accounts = seed_accounts()
    seed_forecast_days(accounts.system)

    run_spec(
        "social-auth.spec.ts",
        web_gateway,
        BROWSER_EMAIL=accounts.system.member.email,
        BROWSER_PASSWORD=accounts.password,
    )

    assert not SocialAccount.objects.exists()
