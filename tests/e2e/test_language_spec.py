import pytest

from pyracast.accounts.models import UserLanguagePreference
from tests.support.browser import run_spec, seed_accounts

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])


def test_the_account_language_follows_the_member_to_another_browser(web_origin: str) -> None:
    accounts = seed_accounts()
    member = accounts.system.member

    run_spec(
        "language.spec.ts",
        web_origin,
        BROWSER_EMAIL=member.email,
        BROWSER_PASSWORD=accounts.password,
    )

    assert UserLanguagePreference.objects.filter(user=member).exists()
