import pytest
from allauth.account.models import EmailAddress

from tests.support.browser import PASSWORD, Gateway, run_spec, seed_accounts, seed_other_workspace

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])


def test_the_next_account_signed_in_on_a_device_sees_none_of_the_previous_one(
    web_gateway: Gateway,
) -> None:
    accounts = seed_accounts()
    other = seed_other_workspace()
    other.owner.email = "other-owner@example.com"
    other.owner.set_password(PASSWORD)
    other.owner.save(update_fields=["email", "password"])
    EmailAddress.objects.create(
        user=other.owner, email=other.owner.email, primary=True, verified=True
    )

    run_spec(
        "session-document.spec.ts",
        web_gateway,
        BROWSER_EMAIL=accounts.system.member.email,
        BROWSER_OTHER_EMAIL=other.owner.email,
        BROWSER_PASSWORD=accounts.password,
    )
